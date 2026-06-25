"""
MeshDNS Master Benchmark Automation Suite (Updated for ACSAC Artifacts)
Runs comprehensive UDP-based tests on the 5-node ESP8266 hardware testbed.
"""

import argparse
import csv
import socket
import threading
import time
import json
import os
import re
import statistics
import ipaddress
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# --- Configuration ---
DEFAULT_TELEMETRY_PORT = 8080
DEFAULT_COMMAND_PORT = 8081
DEFAULT_BROADCAST_IP = "192.168.1.255"
DEFAULT_TARGET_DOMAIN = "lab-target.local"
DEFAULT_TARGET_IP = "192.168.1.10"
DEFAULT_EXPECTED_NODES = 5
DEFAULT_CONTROL_TOKEN = "CHANGE_ME_TESTBED_CONTROL_TOKEN"
DEFAULT_TELEMETRY_INTERVAL_MS = 10000
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[1] / "results"
DEFAULT_BFT_ATTEMPTS = 3
DEFAULT_BFT_RESOLVE_TIMEOUT = 22.0
DEFAULT_BFT_SETTLE_SEC = 2.0
DEFAULT_BFT_STABLE_SEC = 12.0
CONFIG_TOKEN_PATTERN = re.compile(r'^\s*#define\s+TESTBED_CONTROL_TOKEN\s+"([^"]*)"', re.MULTILINE)

TELEMETRY_PORT = DEFAULT_TELEMETRY_PORT
COMMAND_PORT = DEFAULT_COMMAND_PORT
BROADCAST_IP = DEFAULT_BROADCAST_IP
TARGET_DOMAIN = DEFAULT_TARGET_DOMAIN
TARGET_IP = DEFAULT_TARGET_IP
BFT_ATTEMPTS = DEFAULT_BFT_ATTEMPTS
BFT_RESOLVE_TIMEOUT = DEFAULT_BFT_RESOLVE_TIMEOUT
BFT_SETTLE_SEC = DEFAULT_BFT_SETTLE_SEC
BFT_STABLE_SEC = DEFAULT_BFT_STABLE_SEC

# Optional comma-separated allowlist, useful when an old/extra ESP is still
# online but should not participate in the paper benchmark.
# Example:
#   MESHDNS_NODES=192.168.1.21,192.168.1.22,192.168.1.23,192.168.1.24
NODE_ALLOWLIST = {
    ip.strip()
    for ip in os.environ.get("MESHDNS_NODES", "").split(",")
    if ip.strip()
}
PREFERRED_RESOLVER = os.environ.get("MESHDNS_RESOLVER", "").strip()

# Security Token required by firmware
CONTROL_TOKEN = DEFAULT_CONTROL_TOKEN


class BenchmarkOrchestrator:
    def __init__(self, results_root: Path = DEFAULT_RESULTS_ROOT, run_label: str = "hardware"):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.results_dir = results_root / f"{run_label}_{stamp}"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.active_nodes = set()
        self.telemetry_log = []
        self.listener_active = True

        # Sockets
        self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.recv_sock.bind(("0.0.0.0", TELEMETRY_PORT))
        except OSError:
            print("[!] Port 8080 in use. Run 'sudo fuser -k 8080/udp' and try again.")
            exit(1)
        self.recv_sock.settimeout(1.0)

        self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def start_listener(self):
        def listen():
            while self.listener_active:
                try:
                    data, addr = self.recv_sock.recvfrom(1024)
                    payload = data.decode('utf-8', errors='replace').strip()
                    try:
                        node_id, esp_millis, event_type, msg = payload.split('|', 3)
                        # Always use the UDP socket's source address as the node
                        # identifier.  The firmware embeds ip= in some messages (e.g.
                        # HEARTBEAT → node's own IP; CACHE_SEEDED / RESOLVE_OK →
                        # the *resolved* IP), so _message_ip() would return the wrong
                        # address for non-heartbeat events and break all event matching.
                        node_ip = addr[0]
                        if NODE_ALLOWLIST and node_ip not in NODE_ALLOWLIST:
                            continue
                        self.active_nodes.add(node_ip)
                        self.telemetry_log.append({
                            "time": time.time(),
                            "ip": node_ip,
                            "node_id": node_id,
                            "event": event_type,
                            "msg": msg,
                            "fields": self.parse_fields(msg),
                        })
                    except ValueError:
                        pass
                except socket.timeout:
                    continue

        t = threading.Thread(target=listen, daemon=True)
        t.start()

    @staticmethod
    def parse_fields(msg: str) -> Dict[str, str]:
        fields = {}
        for part in msg.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        return fields

    @staticmethod
    def _message_ip(msg: str, fallback_ip: str) -> str:
        return BenchmarkOrchestrator.parse_fields(msg).get("ip", fallback_ip)

    def send_cmd(self, command: str, arg: str = "", target_ip: str = BROADCAST_IP):
        """Matches the exact wire format of testbed_command_center.py"""
        packet = f"{CONTROL_TOKEN}|{command}"
        if arg:
            packet += f"|{arg}"
        self.send_sock.sendto(packet.encode('utf-8'),
                              (target_ip, COMMAND_PORT))
        time.sleep(0.05)

    def discover_devices(self, expected_nodes=DEFAULT_EXPECTED_NODES, timeout_sec=25):
        print(
            f"\n[*] Discovering ESP8266 fleet (Waiting up to {timeout_sec}s for heartbeats)...")
        self.active_nodes.clear()

        if NODE_ALLOWLIST:
            expected_nodes = len(NODE_ALLOWLIST)
            print(f"[*] Restricting benchmark to MESHDNS_NODES={sorted(NODE_ALLOWLIST)}")

        start_time = time.time()
        while len(self.active_nodes) < expected_nodes and (time.time() - start_time) < timeout_sec:
            time.sleep(0.5)

        nodes = sorted(self.active_nodes, key=lambda ip: ipaddress.ip_address(ip))
        print(f"[+] Found {len(nodes)} nodes: {nodes}")
        return nodes

    def latest_heartbeat_by_ip(self) -> Dict[str, Dict]:
        latest = {}
        for entry in self.telemetry_log:
            if entry.get("event") == "HEARTBEAT":
                latest[entry["ip"]] = entry
        return latest

    def wait_for_stable_fleet(
        self,
        nodes: List[str],
        stable_sec: float = DEFAULT_BFT_STABLE_SEC,
        min_peers: Optional[int] = None,
        timeout_sec: float = 45.0,
    ) -> bool:
        if not nodes:
            return False
        if min_peers is None:
            min_peers = max(0, len(nodes) - 2)

        print(
            f"[*] Waiting for stable fleet: {len(nodes)} nodes, "
            f">={min_peers} peers each for {stable_sec:.0f}s..."
        )
        deadline = time.time() + timeout_sec
        stable_start = None
        while time.time() < deadline:
            now = time.time()
            latest = self.latest_heartbeat_by_ip()
            ready = []
            not_ready = []
            for node in nodes:
                hb = latest.get(node)
                if not hb:
                    not_ready.append(f"{node}:no_heartbeat")
                    continue
                age = now - hb["time"]
                try:
                    peers = int(hb.get("fields", {}).get("peers", "-1"))
                except ValueError:
                    peers = -1
                if age <= 12.0 and peers >= min_peers:
                    ready.append(node)
                else:
                    not_ready.append(f"{node}:age={age:.1f}s,peers={peers}")

            if len(ready) == len(nodes):
                if stable_start is None:
                    stable_start = now
                elif now - stable_start >= stable_sec:
                    print("[+] Fleet stable.")
                    return True
            else:
                stable_start = None

            if not_ready:
                print(f"  [wait] {', '.join(not_ready[:3])}" + (" ..." if len(not_ready) > 3 else ""))
            time.sleep(1.0)

        print("[!] Fleet did not reach the requested stability window.")
        return False

    def wait_for_event(self, target_ip: str, target_event: str, start_index: int, timeout=5.0) -> Optional[Dict]:
        return self.wait_for_events(target_ip, [target_event], start_index, timeout)

    def wait_for_events(self, target_ip: str, target_events: Iterable[str], start_index: int, timeout=5.0) -> Optional[Dict]:
        """Updated to accept a start_index to prevent race conditions!"""
        event_set = set(target_events)
        start_time = time.time()
        while time.time() - start_time < timeout:
            # We check the log from the EXACT moment before the command was sent
            for i in range(start_index, len(self.telemetry_log)):
                entry = self.telemetry_log[i]
                if entry['ip'] == target_ip and entry['event'] in event_set:
                    return entry
            time.sleep(0.05)
        return None

    @staticmethod
    def latency_ms(entry: Dict) -> Optional[float]:
        try:
            return float(entry.get("fields", {}).get("latency_ms", ""))
        except ValueError:
            return None

    def choose_requester_and_voters(self, nodes: List[str], avoid_requester: Optional[str] = None) -> tuple[str, List[str]]:
        if PREFERRED_RESOLVER:
            if PREFERRED_RESOLVER not in nodes:
                raise ValueError(f"MESHDNS_RESOLVER={PREFERRED_RESOLVER} was not discovered")
            requester = PREFERRED_RESOLVER
        else:
            candidates = [node for node in nodes if node != avoid_requester]
            if not candidates:
                raise ValueError("No honest requester candidate available")
            requester = candidates[-1]
        voters = [node for node in nodes if node != requester]
        return requester, voters

    def run_mdns_baseline(self, iterations=20):
        print("\n" + "="*60)
        print("TEST 1: STANDARD mDNS BASELINE (OS CACHE)")
        print("="*60)

        times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            try:
                socket.gethostbyname(TARGET_DOMAIN)
                dt = (time.perf_counter() - t0) * 1000
                times.append(dt)
            except socket.gaierror:
                pass
            time.sleep(0.1)

        avg = statistics.mean(times) if times else 0
        print(f"  -> mDNS Average Latency: {avg:.2f} ms")
        return {"protocol": "mDNS", "avg_ms": avg, "raw": times}

    def run_meshdns_warm_cache(self, nodes, iterations=10):
        print("\n" + "="*60)
        print("TEST 2: MESHDNS WARM CACHE (LOCAL HIT)")
        print("="*60)

        requester = nodes[0]
        self.send_cmd("CMD_CLEAR_CACHE", target_ip=BROADCAST_IP)
        time.sleep(1)

        self.send_cmd("CMD_SEED_CACHE",
                      arg=f"{TARGET_DOMAIN}={TARGET_IP}", target_ip=requester)
        time.sleep(0.5)

        times = []
        for _ in range(iterations):
            # 1. Capture log index BEFORE sending to beat the 0.4ms ESP8266 reply
            start_idx = len(self.telemetry_log)

            # 2. Send the command
            self.send_cmd("CMD_RESOLVE", arg=TARGET_DOMAIN,
                          target_ip=requester)

            # 3. Wait for the event using our captured index
            res = self.wait_for_event(
                requester, "RESOLVE_OK", start_index=start_idx)

            if res:
                try:
                    lat_str = [x for x in res['msg'].split(
                        ',') if 'latency_ms' in x][0]
                    lat = float(lat_str.split('=')[1])
                    times.append(lat)
                except Exception:
                    pass
            time.sleep(0.5)

        avg = statistics.mean(times) if times else 0
        print(f"  -> Warm Cache Average Latency: {avg:.2f} ms")
        return {"protocol": "MeshDNS_Warm", "avg_ms": avg, "raw": times}

    def run_meshdns_cold_bft(
        self,
        nodes,
        avoid_requester: Optional[str] = None,
        attempts: int = 3,
        resolve_timeout: float = 10.0,
        settle_sec: float = 2.0,
    ):
        print("\n" + "="*60)
        print("TEST 3: MESHDNS COLD CACHE (BFT QUORUM)")
        print("="*60)

        if len(nodes) < 4:
            print("[-] Need at least 4 nodes for BFT test. Skipping.")
            return None

        try:
            requester, voters = self.choose_requester_and_voters(nodes, avoid_requester=avoid_requester)
        except ValueError as exc:
            print(f"[-] {exc}")
            return None

        attempt_results = []
        for attempt_no in range(1, attempts + 1):
            print(f"\n[*] BFT attempt {attempt_no}/{attempts}")
            self.send_cmd("CMD_CLEAR_CACHE", target_ip=BROADCAST_IP)
            time.sleep(settle_sec)  # Let the fleet clear memory

            seeded_voters = 0
            failed_seed = []
            for voter in voters:
                confirmed = False
                for seed_attempt in range(3):
                    seed_start = len(self.telemetry_log)
                    self.send_cmd("CMD_SEED_CACHE",
                                  arg=f"{TARGET_DOMAIN}={TARGET_IP}", target_ip=voter)
                    ack = self.wait_for_event(voter, "CACHE_SEEDED", seed_start, timeout=3.0)
                    if ack:
                        confirmed = True
                        break
                    if seed_attempt < 2:
                        print(f"  [!] No CACHE_SEEDED from {voter}, retrying...")
                        time.sleep(0.3)

                if confirmed:
                    seeded_voters += 1
                    print(f"  [+] Cache seeded on {voter}")
                else:
                    failed_seed.append(voter)
                    print(f"  [!] Could not confirm seed on {voter} - BFT quorum may fail")
                time.sleep(0.5)

            print(f"[*] Seeded {seeded_voters}/{len(voters)} voters")
            if failed_seed:
                print(f"[!] Seed failed on: {', '.join(failed_seed)}")
            time.sleep(settle_sec)  # Let the network settle

            print(f"[*] Triggering resolution on unseeded node {requester}...")

            # Capture index before triggering
            start_idx = len(self.telemetry_log)
            self.send_cmd("CMD_RESOLVE", arg=TARGET_DOMAIN, target_ip=requester)

            res = self.wait_for_events(
                requester,
                ["RESOLVE_OK", "RESOLVE_FAIL"],
                start_index=start_idx,
                timeout=resolve_timeout,
            )

            attempt_result = {
                "attempt": attempt_no,
                "seeded_voters": seeded_voters,
                "failed_seed": failed_seed,
                "event": res["event"] if res else "TIMEOUT",
                "message": res["msg"] if res else "",
                "success": False,
            }
            attempt_results.append(attempt_result)

            if res and res["event"] == "RESOLVE_OK":
                source = res.get("fields", {}).get("source", "")
                if source != "peer_quorum":
                    print(f"[-] Resolve succeeded via {source or 'unknown'}, not BFT peer quorum.")
                    print(f"  -> Details: {res['msg']}")
                else:
                    try:
                        lat = self.latency_ms(res)
                        if lat is None:
                            raise ValueError("missing latency")
                        print(f"  -> Cold Cache (BFT) Latency: {lat:.2f} ms")
                        print(f"  -> Details: {res['msg']}")
                        attempt_result["success"] = True
                        return {
                            "protocol": "MeshDNS_Cold_BFT",
                            "avg_ms": lat,
                            "raw": [lat],
                            "resolver": requester,
                            "voters": voters,
                            "details": res["msg"],
                            "attempts": attempt_results,
                        }
                    except Exception:
                        print(f"[-] Could not parse BFT latency: {res['msg']}")

            elif res and res["event"] == "RESOLVE_FAIL":
                print("[-] BFT Resolution Failed.")
                print(f"  -> Details: {res['msg']}")
            else:
                print("[-] BFT Resolution timed out without RESOLVE telemetry.")

            if attempt_no < attempts:
                print(f"[*] Retrying after {settle_sec:.1f}s settle...")
                time.sleep(settle_sec)

        print("[-] BFT Resolution Failed after all attempts.")
        return {
            "protocol": "MeshDNS_Cold_BFT",
            "avg_ms": 0,
            "raw": [],
            "resolver": requester,
            "voters": voters,
            "details": "failed_after_retries",
            "attempts": attempt_results,
        }

    def run_meshdns_stress_test(self, nodes, total_requests=500, delay_ms=20):
        print("\n" + "="*60)
        print("TEST 5: THROUGHPUT & STRESS TEST (WARM CACHE)")
        print("="*60)

        target_node = nodes[0]
        self.send_cmd("CMD_CLEAR_CACHE", target_ip=BROADCAST_IP)
        time.sleep(1)

        # Confirm the seed landed before unleashing the barrage - if the cache
        # is cold every one of the 500 CMD_RESOLVE calls triggers a full BFT
        # round-trip which overwhelms the node and produces 0 successes.
        seed_confirmed = False
        for attempt in range(3):
            seed_start = len(self.telemetry_log)
            self.send_cmd("CMD_SEED_CACHE",
                          arg=f"{TARGET_DOMAIN}={TARGET_IP}", target_ip=target_node)
            ack = self.wait_for_event(target_node, "CACHE_SEEDED", seed_start, timeout=2.0)
            if ack:
                seed_confirmed = True
                print(f"[+] Cache seeded on {target_node} (attempt {attempt + 1})")
                break
            print(f"[!] No CACHE_SEEDED from {target_node} (attempt {attempt + 1}), retrying...")
            time.sleep(0.3)

        if not seed_confirmed:
            print(f"[!] WARNING: Could not confirm seed on {target_node}. "
                  "Stress results will likely be 0 - consider restarting nodes.")
        time.sleep(0.5)

        # send_cmd has a built-in 50 ms sleep; the effective gap per request is
        # delay_ms + 50 ms, so the actual injection rate is lower than delay_ms alone.
        effective_delay_ms = delay_ms + 50
        target_qps = 1000 / effective_delay_ms
        print(
            f"[*] Bombarding {target_node} with {total_requests} requests...")
        print(f"[*] Target injection rate: ~{target_qps:.0f} Queries/Sec "
              f"(effective {effective_delay_ms} ms/req)")

        start_index = len(self.telemetry_log)
        start_time = time.time()

        for i in range(total_requests):
            self.send_cmd("CMD_RESOLVE", arg=TARGET_DOMAIN,
                          target_ip=target_node)
            time.sleep(delay_ms / 1000.0)

        send_end_time = time.time()

        print("[*] Barrage complete. Waiting 5 seconds for final telemetry packets...")
        time.sleep(5.0)

        successes = 0
        latencies = []

        for i in range(start_index, len(self.telemetry_log)):
            entry = self.telemetry_log[i]
            if entry['ip'] == target_node and entry['event'] == 'RESOLVE_OK':
                successes += 1
                try:
                    lat_str = [x for x in entry['msg'].split(
                        ',') if 'latency_ms' in x][0]
                    latencies.append(float(lat_str.split('=')[1]))
                except Exception:
                    pass

        actual_duration = send_end_time - start_time
        qps = successes / actual_duration if actual_duration > 0 else 0
        success_rate = (successes / total_requests) * 100
        traffic = estimate_meshdns_traffic(
            request_count=total_requests,
            duration_s=actual_duration,
            node_count=len(nodes),
            success_count=successes,
        )

        print(f"  -> Total Requests Sent: {total_requests}")
        print(f"  -> Total Successful Resolutions: {successes}")
        print(f"  -> Success Rate: {success_rate:.1f}%")
        print(f"  -> Actual Throughput: {qps:.1f} Queries Per Second (QPS)")
        print(f"  -> Estimated MeshDNS command traffic: {traffic['command_kbps']:.2f} kbps")
        print(f"  -> Estimated warm-cache telemetry traffic: {traffic['telemetry_kbps']:.2f} kbps")

        avg_lat = sum(latencies)/len(latencies) if latencies else 0
        if latencies:
            print(f"  -> Avg Latency under load: {avg_lat:.2f} ms")

        return {
            "protocol": "MeshDNS_Stress",
            "total_sent": total_requests,
            "successes": successes,
            "success_rate": success_rate,
            "qps": qps,
            "avg_latency_ms": avg_lat,
            "traffic_estimate": traffic,
        }

    def run_byzantine_prompt(self, nodes):
        print("\n" + "="*60)
        print("TEST 4: BYZANTINE ADVERSARY EVALUATION")
        print("="*60)
        print("[!] ACTION REQUIRED:")
        print("1. Keep this script running.")
        print("2. Flash exactly ONE node with '#define BYZANTINE_MODE 1'")
        print("3. Ensure the other nodes remain honest.")
        input("Press ENTER when the Byzantine node is booted and online...")
        byzantine_ip = input("Enter Byzantine node IP (blank if unknown): ").strip() or None

        print("\n[*] Re-discovering fleet...")
        nodes = self.discover_devices()
        self.wait_for_stable_fleet(nodes, stable_sec=BFT_STABLE_SEC)
        return self.run_meshdns_cold_bft(
            nodes,
            avoid_requester=byzantine_ip,
            attempts=BFT_ATTEMPTS,
            resolve_timeout=BFT_RESOLVE_TIMEOUT,
            settle_sec=BFT_SETTLE_SEC,
        )

    def save_results(self, data):
        json_path = self.results_dir / "meshdns_evaluation.json"
        with json_path.open('w') as f:
            json.dump(data, f, indent=2)

        telemetry_path = self.results_dir / "telemetry_log.csv"
        with telemetry_path.open("w", newline="") as f:
            fieldnames = ["time", "ip", "node_id", "event", "msg"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for entry in self.telemetry_log:
                writer.writerow({key: entry.get(key, "") for key in fieldnames})

        summary_path = self.results_dir / "mdns_meshdns_summary.csv"
        write_comparison_summary(data, summary_path)

        print(f"\n[+] Full evaluation saved to: {json_path}")
        print(f"[+] Telemetry CSV saved to: {telemetry_path}")
        print(f"[+] Comparison summary saved to: {summary_path}")

    def stop(self):
        self.listener_active = False
        self.recv_sock.close()
        self.send_sock.close()


def parse_nodes(value: str) -> set[str]:
    return {ip.strip() for ip in value.split(",") if ip.strip()}


def read_config_token(config_path: Path) -> Optional[str]:
    try:
        text = config_path.read_text()
    except OSError:
        return None
    match = CONFIG_TOKEN_PATTERN.search(text)
    return match.group(1) if match else None


def resolve_token(args: argparse.Namespace) -> str:
    token = getattr(args, "token", None)
    if token:
        return token
    config_path = getattr(args, "config", None)
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / "firmware" / "meshdns_node" / "config.h"
    config_token = read_config_token(config_path)
    if config_token:
        return config_token
    return os.environ.get("MESHDNS_CONTROL_TOKEN", DEFAULT_CONTROL_TOKEN)


def estimate_meshdns_traffic(request_count: int, duration_s: float, node_count: int,
                             success_count: int) -> Dict[str, float]:
    """Conservative on-air byte estimate for reviewer bandwidth discussion."""
    duration_s = max(duration_s, 0.001)
    command_payload_bytes = len(f"{CONTROL_TOKEN}|CMD_RESOLVE|{TARGET_DOMAIN}".encode("utf-8"))
    telemetry_payload_bytes = len(
        f"node|4294967295|RESOLVE_OK|domain={TARGET_DOMAIN},ip={TARGET_IP},source=cache,latency_ms=9999.99"
        .encode("utf-8")
    )
    udp_ipv4_overhead = 28
    wifi_mac_overhead = 34
    per_command_air_bytes = command_payload_bytes + udp_ipv4_overhead + wifi_mac_overhead
    per_telemetry_air_bytes = telemetry_payload_bytes + udp_ipv4_overhead + wifi_mac_overhead
    command_kbps = (request_count * per_command_air_bytes * 8) / duration_s / 1000
    telemetry_kbps = (success_count * per_telemetry_air_bytes * 8) / duration_s / 1000
    heartbeat_payload_bytes = len(
        b"node|4294967295|HEARTBEAT|ip=255.255.255.255,peers=10,cache_used=20,heap=99999"
    )
    heartbeat_air_bytes = heartbeat_payload_bytes + udp_ipv4_overhead + wifi_mac_overhead
    heartbeat_kbps = (node_count * heartbeat_air_bytes * 8) / (DEFAULT_TELEMETRY_INTERVAL_MS / 1000) / 1000
    return {
        "command_payload_bytes": command_payload_bytes,
        "telemetry_payload_bytes": telemetry_payload_bytes,
        "udp_ipv4_overhead_bytes": udp_ipv4_overhead,
        "wifi_mac_overhead_bytes": wifi_mac_overhead,
        "per_command_air_bytes": per_command_air_bytes,
        "per_telemetry_air_bytes": per_telemetry_air_bytes,
        "command_kbps": command_kbps,
        "telemetry_kbps": telemetry_kbps,
        "heartbeat_kbps": heartbeat_kbps,
    }


def write_comparison_summary(data: Dict[str, Any], path: Path) -> None:
    rows = []
    for key in ["mdns", "warm", "cold_honest", "stress_test", "cold_byzantine"]:
        result = data.get(key)
        if not result:
            continue
        row = {
            "scenario": key,
            "protocol": result.get("protocol", key),
            "avg_ms": result.get("avg_ms", result.get("avg_latency_ms", "")),
            "success_rate": result.get("success_rate", ""),
            "qps": result.get("qps", ""),
            "samples": len(result.get("raw", [])) if isinstance(result.get("raw"), list) else "",
        }
        rows.append(row)

    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Canonical MeshDNS hardware benchmark runner")
    parser.add_argument("--broadcast", default=os.environ.get("MESHDNS_BROADCAST", DEFAULT_BROADCAST_IP))
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--command-port", type=int, default=DEFAULT_COMMAND_PORT)
    parser.add_argument("--target-domain", default=os.environ.get("MESHDNS_TARGET_DOMAIN", DEFAULT_TARGET_DOMAIN))
    parser.add_argument("--target-ip", default=os.environ.get("MESHDNS_TARGET_IP", DEFAULT_TARGET_IP))
    parser.add_argument("--expected-nodes", type=int, default=int(os.environ.get("MESHDNS_EXPECTED_NODES", DEFAULT_EXPECTED_NODES)))
    parser.add_argument("--nodes", default=os.environ.get("MESHDNS_NODES", ""),
                        help="Comma-separated node allowlist")
    parser.add_argument("--resolver", default=os.environ.get("MESHDNS_RESOLVER", ""),
                        help="Preferred resolver/requester node IP")
    parser.add_argument("--token", default=None,
                        help="Must match TESTBED_CONTROL_TOKEN in firmware")
    parser.add_argument("--config", type=Path,
                        default=repo_root / "firmware" / "meshdns_node" / "config.h")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--run-label", default="hardware")
    parser.add_argument("--mdns-iterations", type=int, default=20)
    parser.add_argument("--warm-iterations", type=int, default=10)
    parser.add_argument("--stress-requests", type=int, default=500)
    parser.add_argument("--stress-delay-ms", type=int, default=20)
    parser.add_argument("--bft-attempts", type=int, default=DEFAULT_BFT_ATTEMPTS)
    parser.add_argument("--bft-resolve-timeout", type=float, default=DEFAULT_BFT_RESOLVE_TIMEOUT)
    parser.add_argument("--bft-settle-sec", type=float, default=DEFAULT_BFT_SETTLE_SEC)
    parser.add_argument("--bft-stable-sec", type=float, default=DEFAULT_BFT_STABLE_SEC,
                        help="Fleet stability window after manual Byzantine reflash")
    parser.add_argument("--discover-timeout", type=int, default=25)
    parser.add_argument("--skip-byzantine-prompt", action="store_true")
    parser.add_argument("--video-coexistence", default="",
                        help="Optional measured background traffic note, e.g. 'YouTube 4K on laptop X'")
    return parser.parse_args()


def apply_args(args: argparse.Namespace) -> None:
    """Apply CLI/env settings to module-level bench configuration.

    Tolerates partial namespaces (e.g. bft_benchmark/adversarial_benchmark) by
    falling back to defaults for BFT timing fields not present on every parser.
    """
    global TELEMETRY_PORT, COMMAND_PORT, BROADCAST_IP, TARGET_DOMAIN, TARGET_IP
    global NODE_ALLOWLIST, PREFERRED_RESOLVER, CONTROL_TOKEN
    global BFT_ATTEMPTS, BFT_RESOLVE_TIMEOUT, BFT_SETTLE_SEC, BFT_STABLE_SEC

    TELEMETRY_PORT = getattr(args, "telemetry_port", DEFAULT_TELEMETRY_PORT)
    COMMAND_PORT = getattr(args, "command_port", DEFAULT_COMMAND_PORT)
    BROADCAST_IP = getattr(args, "broadcast", DEFAULT_BROADCAST_IP)
    TARGET_DOMAIN = getattr(args, "target_domain", DEFAULT_TARGET_DOMAIN)
    TARGET_IP = getattr(args, "target_ip", DEFAULT_TARGET_IP)

    nodes_value = getattr(args, "nodes", None)
    if nodes_value:
        NODE_ALLOWLIST = parse_nodes(nodes_value)

    resolver = getattr(args, "resolver", None)
    if resolver is not None:
        PREFERRED_RESOLVER = str(resolver).strip()

    CONTROL_TOKEN = resolve_token(args)

    # bft_benchmark uses --attempts; run_all_benchmarks uses --bft-attempts
    bft_attempts = getattr(args, "bft_attempts", None)
    if bft_attempts is None:
        bft_attempts = getattr(args, "attempts", DEFAULT_BFT_ATTEMPTS)
    BFT_ATTEMPTS = max(1, int(bft_attempts))

    BFT_RESOLVE_TIMEOUT = max(
        1.0,
        float(getattr(args, "bft_resolve_timeout", getattr(args, "resolve_timeout_s", DEFAULT_BFT_RESOLVE_TIMEOUT))),
    )
    BFT_SETTLE_SEC = max(0.0, float(getattr(args, "bft_settle_sec", getattr(args, "settle_s", DEFAULT_BFT_SETTLE_SEC))))
    BFT_STABLE_SEC = max(0.0, float(getattr(args, "bft_stable_sec", DEFAULT_BFT_STABLE_SEC)))


def main() -> None:
    args = parse_args()
    apply_args(args)

    bench = BenchmarkOrchestrator(results_root=args.results_root, run_label=args.run_label)
    bench.start_listener()

    try:
        nodes = bench.discover_devices(expected_nodes=args.expected_nodes, timeout_sec=args.discover_timeout)
        if not nodes:
            print("[-] No nodes found. Exiting.")
            bench.stop()
            raise SystemExit(1)

        results = {
            "metadata": {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "broadcast": BROADCAST_IP,
                "telemetry_port": TELEMETRY_PORT,
                "command_port": COMMAND_PORT,
                "target_domain": TARGET_DOMAIN,
                "target_ip": TARGET_IP,
                "nodes": nodes,
                "expected_nodes": args.expected_nodes,
                "node_allowlist": sorted(NODE_ALLOWLIST),
                "preferred_resolver": PREFERRED_RESOLVER,
                "bft_attempts": BFT_ATTEMPTS,
                "bft_resolve_timeout": BFT_RESOLVE_TIMEOUT,
                "bft_settle_sec": BFT_SETTLE_SEC,
                "bft_stable_sec": BFT_STABLE_SEC,
                "video_coexistence": args.video_coexistence,
            }
        }
        if args.video_coexistence:
            print(f"[*] Background traffic note: {args.video_coexistence}")
            print("[*] Keep the stream running for the whole benchmark and archive this result folder.")

        results['mdns'] = bench.run_mdns_baseline(iterations=args.mdns_iterations)
        results['warm'] = bench.run_meshdns_warm_cache(nodes, iterations=args.warm_iterations)
        results['cold_honest'] = bench.run_meshdns_cold_bft(
            nodes,
            attempts=BFT_ATTEMPTS,
            resolve_timeout=BFT_RESOLVE_TIMEOUT,
            settle_sec=BFT_SETTLE_SEC,
        )

        if args.skip_byzantine_prompt:
            print("\n[*] Skipping manual Byzantine firmware test.")
        else:
            ans = input("\nRun manual Byzantine firmware test? (y/n): ")
            if ans.lower() == 'y':
                results['cold_byzantine'] = bench.run_byzantine_prompt(nodes)

        print("\n[*] Allowing 5 seconds for the fleet to settle before stress test...")
        time.sleep(5)

        results['stress_test'] = bench.run_meshdns_stress_test(
            nodes, total_requests=args.stress_requests, delay_ms=args.stress_delay_ms)

        bench.save_results(results)

    except KeyboardInterrupt:
        print("\n[*] Benchmark aborted.")
    finally:
        bench.stop()


if __name__ == "__main__":
    main()
