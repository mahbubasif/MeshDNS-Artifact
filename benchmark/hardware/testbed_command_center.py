#!/usr/bin/env python3
"""
MeshDNS command center for ESP8266 hardware tests.

Runs on any benchmark host on the same LAN as the ESP8266 nodes. It listens for UDP
telemetry from nodes, writes CSV logs, and sends authenticated benchmark
commands to either the full fleet or one target ESP8266 IP.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shlex
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_TELEMETRY_PORT = 8080
DEFAULT_COMMAND_PORT = 8081
DEFAULT_BROADCAST_IP = "192.168.1.255"
DEFAULT_TARGET_DOMAIN = "lab-target.local"
DEFAULT_TARGET_IP = "192.168.1.10"
DEFAULT_OUTPUT = "meshdns_4node_benchmark.csv"
DEFAULT_TOKEN = "CHANGE_ME_TESTBED_CONTROL_TOKEN"
CONFIG_TOKEN_PATTERN = re.compile(
    r'^\s*#define\s+TESTBED_CONTROL_TOKEN\s+"([^"]*)"', re.MULTILINE)


@dataclass
class NodeState:
    node_id: str
    ip: str
    last_seen: float
    last_event: str
    fields: Dict[str, str] = field(default_factory=dict)


class CommandCenter:
    def __init__(
        self,
        telemetry_port: int,
        command_port: int,
        broadcast_ip: str,
        token: str,
        output: Path,
    ) -> None:
        self.telemetry_port = telemetry_port
        self.command_port = command_port
        self.broadcast_ip = broadcast_ip
        self.token = token
        self.output = output
        self.nodes: Dict[str, NodeState] = {}
        self.lock = threading.Lock()
        self.event_cv = threading.Condition(self.lock)
        self.events: List[Dict[str, object]] = []
        self.stop_event = threading.Event()

        self.command_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.command_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def start_listener(self) -> threading.Thread:
        thread = threading.Thread(target=self._telemetry_listener, daemon=True)
        thread.start()
        return thread

    def _telemetry_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.telemetry_port))

        self.output.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.output.exists() or self.output.stat().st_size == 0

        with self.output.open("a", newline="") as file:
            writer = csv.writer(file)
            if is_new:
                writer.writerow(
                    ["host_time", "node_ip", "node_id",
                        "esp_millis", "event", "message"]
                )

            while not self.stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except OSError:
                    break

                host_time = datetime.now().isoformat(timespec="milliseconds")
                payload = data.decode("utf-8", errors="replace").strip()
                parsed = self._parse_payload(payload)
                if parsed is None:
                    continue

                node_id, esp_millis, event, message = parsed
                fields = self._parse_fields(message)
                node_ip = addr[0]

                with self.lock:
                    self.nodes[node_id] = NodeState(
                        node_id=node_id,
                        ip=node_ip,
                        last_seen=time.time(),
                        last_event=event,
                        fields=fields,
                    )
                    self.events.append(
                        {
                            "time": time.time(),
                            "node_id": node_id,
                            "ip": node_ip,
                            "event": event,
                            "message": message,
                            "fields": fields,
                        }
                    )
                    self.event_cv.notify_all()

                writer.writerow([host_time, addr[0], node_id,
                                esp_millis, event, message])
                file.flush()
                self._print_event(node_id, node_ip, event, message)

    @staticmethod
    def _parse_payload(payload: str) -> Optional[tuple[str, str, str, str]]:
        parts = payload.split("|", 3)
        if len(parts) != 4:
            return None
        return parts[0], parts[1], parts[2], parts[3]

    @staticmethod
    def _parse_fields(message: str) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for part in message.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        return fields

    @staticmethod
    def _print_event(node_id: str, ip: str, event: str, message: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now}] {node_id}@{ip} {event}: {message}")
        print("MeshDNS> ", end="", flush=True)

    def send_command(self, command: str, arg: str = "", target: str = "all") -> None:
        destination = self.broadcast_ip if target == "all" else target
        packet = f"{self.token}|{command}"
        if arg:
            packet += f"|{arg}"
        self.command_sock.sendto(packet.encode(
            "utf-8"), (destination, self.command_port))
        print(f"sent {command} to {destination}" +
              (f" ({arg})" if arg else ""))

    def event_count(self) -> int:
        with self.lock:
            return len(self.events)

    def wait_for_resolve(self, target_ip: str, start_index: int, timeout: float = 12.0) -> Optional[Dict[str, object]]:
        deadline = time.time() + timeout
        with self.event_cv:
            while True:
                for event in self.events[start_index:]:
                    if event["ip"] != target_ip:
                        continue
                    if event["event"] in {"RESOLVE_OK", "RESOLVE_FAIL"}:
                        return event

                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.event_cv.wait(timeout=remaining)

    def resolve_and_report(self, domain: str, target_ip: str, timeout: float = 12.0) -> Optional[Dict[str, object]]:
        start_index = self.event_count()
        self.send_command("CMD_RESOLVE", arg=domain, target=target_ip)
        result = self.wait_for_resolve(target_ip, start_index, timeout=timeout)
        if result is None:
            print(f"resolve timed out on {target_ip} after {timeout:.1f}s")
            return None

        fields = result.get("fields", {})
        source = fields.get("source", "unknown") if isinstance(
            fields, dict) else "unknown"
        latency = fields.get("latency_ms", "?") if isinstance(
            fields, dict) else "?"
        ip = fields.get("ip", "?") if isinstance(fields, dict) else "?"
        print(
            f"resolve result from {target_ip}: event={result['event']} "
            f"ip={ip} source={source} latency_ms={latency}"
        )
        self.explain_resolve_source(source)
        return result

    @staticmethod
    def explain_resolve_source(source: str) -> None:
        explanations = {
            "cache": "local cache hit on this resolver",
            "peer_quorum": "answer accepted from peer votes, then cached locally on this resolver",
            "root_doh_ad_checked": "upstream DNS accepted by authenticated DoH/AD cross-check, then cached locally",
            "root_doh_ad_unchecked": "upstream DNS returned but DoH/AD cross-check was unavailable or rejected cache use",
            "peer_failed": "peer quorum failed and .local upstream fallback was disabled",
            "failed": "resolution failed",
        }
        print(
            f"source meaning: {explanations.get(source, 'see telemetry detail field')}")

    def run_cache_flow(self, domain: str, first_ip: str, second_ip: str, timeout: float = 12.0) -> None:
        print(f"Step 1: resolving {domain} on first node {first_ip}")
        first = self.resolve_and_report(domain, first_ip, timeout=timeout)
        if first is None or first["event"] != "RESOLVE_OK":
            print("first resolve did not succeed; skipping second node check")
            return

        time.sleep(0.5)
        print(f"Step 2: resolving {domain} on second node {second_ip}")
        second = self.resolve_and_report(domain, second_ip, timeout=timeout)
        if second is None:
            return

        fields = second.get("fields", {})
        source = fields.get("source", "unknown") if isinstance(
            fields, dict) else "unknown"
        if source == "cache":
            print("Interpretation: second node already had a local cache entry.")
        elif source == "peer_quorum":
            print(
                "Interpretation: second node learned the answer from peer quorum and cached it locally.")
        elif source.startswith("root_dns"):
            print(
                "Interpretation: second node did not get enough cached peer votes and used upstream DNS.")
        else:
            print("Interpretation: inspect the telemetry detail field for this source.")

    @staticmethod
    def parse_timeout(value: str, default: float = 12.0) -> float:
        try:
            return max(0.1, float(value))
        except ValueError:
            print(f"invalid timeout '{value}', using {default:.1f}s")
            return default

    def print_nodes(self) -> None:
        with self.lock:
            nodes = list(self.nodes.values())

        if not nodes:
            print("No nodes seen yet. Wait for heartbeats or run: discover")
            return

        print("Seen nodes:")
        for node in sorted(nodes, key=lambda n: n.ip):
            age = time.time() - node.last_seen
            peers = node.fields.get("peers", "?")
            cache = node.fields.get("cache_used", "?")
            heap = node.fields.get("heap", "?")
            print(
                f"  {node.node_id:>8}  {node.ip:<15} "
                f"age={age:5.1f}s peers={peers} cache={cache} heap={heap} "
                f"last={node.last_event}"
            )

    def run_bft_cold(self, domain: str, ip: str, resolver_ip: str) -> None:
        with self.lock:
            seed_ips = sorted(
                node.ip for node in self.nodes.values()
                if node.ip != resolver_ip
            )

        if len(seed_ips) < 3:
            print(
                "Need at least 3 known seed nodes. Run 'nodes' and wait for all heartbeats first.")
            return

        print(
            f"Running cold BFT benchmark: domain={domain}, ip={ip}, resolver={resolver_ip}")
        print(f"Seed nodes: {', '.join(seed_ips)}")

        self.send_command("CMD_CLEAR_CACHE", target="all")
        time.sleep(1.5)

        for seed_ip in seed_ips:
            self.send_command("CMD_SEED_CACHE",
                              arg=f"{domain}={ip}", target=seed_ip)
            time.sleep(0.3)

        time.sleep(1.0)
        self.send_command("CMD_RESOLVE", arg=domain, target=resolver_ip)

    def repl(self) -> None:
        print("MeshDNS Command Center")
        print(
            f"Telemetry UDP: {self.telemetry_port}, command UDP: {self.command_port}")
        print(f"Broadcast target: {self.broadcast_ip}")
        print(
            f"Testbed target: {DEFAULT_TARGET_DOMAIN} -> {DEFAULT_TARGET_IP}")
        print(f"CSV log: {self.output}")
        print("")
        print("Commands:")
        print("  nodes")
        print("  discover [all|node_ip]")
        print("  peers [all|node_ip]")
        print("  stats [all|node_ip]")
        print("  clear [all|node_ip]")
        print("  seed <domain> <ip> [all|node_ip]")
        print("  resolve <domain> [all|node_ip]")
        print("  resolvewait <domain> <node_ip> [timeout_sec]")
        print(
            "  cacheflow <domain> <first_node_ip> <second_node_ip> [timeout_sec]")
        print("  bftcold <domain> <ip> <resolver_ip>")
        print("  quit")

        while True:
            try:
                line = input("MeshDNS> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                return

            if not line:
                continue

            try:
                args = shlex.split(line)
            except ValueError as exc:
                print(f"parse error: {exc}")
                continue

            verb = args[0].lower()
            if verb in {"quit", "exit"}:
                return
            if verb == "nodes":
                self.print_nodes()
                continue

            if verb in {"discover", "peers", "stats", "clear"}:
                target = args[1] if len(args) > 1 else "all"
                command = {
                    "discover": "CMD_DISCOVER",
                    "peers": "CMD_PEERS",
                    "stats": "CMD_STATS",
                    "clear": "CMD_CLEAR_CACHE",
                }[verb]
                self.send_command(command, target=target)
                continue

            if verb == "resolve":
                if len(args) < 2:
                    print("usage: resolve <domain> [all|node_ip]")
                    continue
                domain = args[1]
                target = args[2] if len(args) > 2 else "all"
                self.send_command("CMD_RESOLVE", arg=domain, target=target)
                continue

            if verb == "resolvewait":
                if len(args) not in {3, 4}:
                    print(
                        "usage: resolvewait <domain> <node_ip> [timeout_sec]")
                    continue
                timeout = self.parse_timeout(
                    args[3]) if len(args) == 4 else 12.0
                self.resolve_and_report(args[1], args[2], timeout=timeout)
                continue

            if verb == "cacheflow":
                if len(args) not in {4, 5}:
                    print(
                        "usage: cacheflow <domain> <first_node_ip> <second_node_ip> [timeout_sec]")
                    continue
                timeout = self.parse_timeout(
                    args[4]) if len(args) == 5 else 12.0
                self.run_cache_flow(args[1], args[2], args[3], timeout=timeout)
                continue

            if verb == "seed":
                if len(args) < 3:
                    print("usage: seed <domain> <ip> [all|node_ip]")
                    continue
                domain = args[1]
                ip = args[2]
                target = args[3] if len(args) > 3 else "all"
                self.send_command("CMD_SEED_CACHE",
                                  arg=f"{domain}={ip}", target=target)
                continue

            if verb == "bftcold":
                if len(args) != 4:
                    print("usage: bftcold <domain> <ip> <resolver_ip>")
                    continue
                self.run_bft_cold(
                    domain=args[1], ip=args[2], resolver_ip=args[3])
                continue

            print(f"unknown command: {verb}")

    def stop(self) -> None:
        self.stop_event.set()
        self.command_sock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MeshDNS ESP8266 command center")
    parser.add_argument("--telemetry-port", type=int,
                        default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--command-port", type=int,
                        default=DEFAULT_COMMAND_PORT)
    parser.add_argument(
        "--broadcast",
        default=os.environ.get("MESHDNS_BROADCAST", DEFAULT_BROADCAST_IP),
        help="Subnet broadcast IP, e.g. 192.168.0.255, 192.168.1.255, or 10.42.0.255",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Must match TESTBED_CONTROL_TOKEN in firmware config.h",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve(
        ).parents[2] / "firmware" / "meshdns_node" / "config.h",
        help="Firmware config.h to read TESTBED_CONTROL_TOKEN from",
    )
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    return parser.parse_args()


def read_config_token(config_path: Path) -> Optional[str]:
    try:
        text = config_path.read_text()
    except OSError:
        return None

    match = CONFIG_TOKEN_PATTERN.search(text)
    if not match:
        return None
    return match.group(1)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


def resolve_token(args: argparse.Namespace) -> tuple[str, str, Optional[str]]:
    env_token = os.environ.get("MESHDNS_CONTROL_TOKEN")
    config_token = read_config_token(args.config)

    if args.token is not None:
        return args.token, "command line --token", config_token
    if config_token:
        return config_token, f"firmware config {args.config}", config_token
    if env_token:
        return env_token, "MESHDNS_CONTROL_TOKEN environment variable", config_token
    return DEFAULT_TOKEN, "built-in default", config_token


def main() -> None:
    args = parse_args()
    token, token_source, config_token = resolve_token(args)

    if os.environ.get("MESHDNS_CONTROL_TOKEN") and config_token and os.environ["MESHDNS_CONTROL_TOKEN"] != config_token:
        print(
            "Warning: MESHDNS_CONTROL_TOKEN differs from firmware config.h; using config.h.")
        print("         Pass --token explicitly if the ESPs were flashed with a different token.")
    if token == DEFAULT_TOKEN:
        print(
            "Warning: using the default testbed token. Change it before real experiments.")
    print(f"Control token source: {token_source}")
    print(f"Control token fingerprint: sha256:{token_fingerprint(token)}")

    center = CommandCenter(
        telemetry_port=args.telemetry_port,
        command_port=args.command_port,
        broadcast_ip=args.broadcast,
        token=token,
        output=args.output,
    )
    center.start_listener()
    try:
        center.repl()
    finally:
        center.stop()


if __name__ == "__main__":
    main()
