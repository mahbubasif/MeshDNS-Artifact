#!/usr/bin/env python3
"""
Focused MeshDNS BFT benchmark runner.

Use this when you only want to exercise cold-cache peer quorum behavior without
running mDNS, warm-cache, and stress tests first. This is especially useful for
Byzantine runs because one missing honest vote can make a 5-node/1-Byzantine
round fail even when the protocol is behaving correctly.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import run_all_benchmarks as benchlib


DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[1] / "results"


def parse_nodes(value: str) -> List[str]:
    return sorted(
        {ip.strip() for ip in value.split(",") if ip.strip()},
        key=lambda ip: tuple(int(part) for part in ip.split(".")),
    )


def parse_fields(msg: str) -> Dict[str, str]:
    fields = {}
    for part in msg.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def parse_detail(msg: str) -> Dict[str, Any]:
    fields = parse_fields(msg)
    detail = fields.get("detail", "")
    for part in detail.split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[f"detail_{key}"] = value
    return fields


def latest_event(
    telemetry: List[Dict[str, Any]],
    ip: str,
    events: Iterable[str],
    start_index: int,
    timeout_s: float,
) -> Optional[Dict[str, Any]]:
    event_set = set(events)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for idx in range(start_index, len(telemetry)):
            entry = telemetry[idx]
            if entry["ip"] == ip and entry["event"] in event_set:
                return entry
        time.sleep(0.05)
    return None


def wait_for_seed(center: benchlib.BenchmarkOrchestrator, voter: str, domain: str, target_ip: str,
                  attempts: int, timeout_s: float) -> bool:
    for attempt in range(1, attempts + 1):
        start = len(center.telemetry_log)
        center.send_cmd("CMD_SEED_CACHE", arg=f"{domain}={target_ip}", target_ip=voter)
        ack = latest_event(center.telemetry_log, voter, ["CACHE_SEEDED"], start, timeout_s)
        if ack:
            print(f"  [+] Seeded {voter} (attempt {attempt})")
            return True
        print(f"  [!] No CACHE_SEEDED from {voter} (attempt {attempt})")
        time.sleep(0.3)
    return False


def choose_resolver(nodes: List[str], resolver: str, byzantine_ip: str) -> str:
    if resolver:
        if resolver not in nodes:
            raise ValueError(f"--resolver {resolver} is not in node list")
        if resolver == byzantine_ip:
            raise ValueError("Resolver must be honest; do not choose the Byzantine node as resolver")
        return resolver

    candidates = [node for node in nodes if node != byzantine_ip]
    if not candidates:
        raise ValueError("No honest resolver candidate found")
    return candidates[-1]


def run_attempt(center: benchlib.BenchmarkOrchestrator, nodes: List[str], args: argparse.Namespace,
                attempt_no: int) -> Dict[str, Any]:
    resolver = choose_resolver(nodes, args.resolver, args.byzantine_ip)
    voters = [node for node in nodes if node != resolver]
    honest_voters = [node for node in voters if node != args.byzantine_ip]

    print("\n" + "=" * 72)
    print(f"BFT ATTEMPT {attempt_no}/{args.attempts}")
    print("=" * 72)
    print(f"Resolver: {resolver}")
    print(f"Voters:   {', '.join(voters)}")
    if args.byzantine_ip:
        print(f"Byzantine voter: {args.byzantine_ip}")
        print(f"Honest voters:   {', '.join(honest_voters)}")
        if len(honest_voters) < args.expected_quorum:
            print(f"[!] Only {len(honest_voters)} honest voters available; quorum {args.expected_quorum} cannot tolerate one Byzantine voter.")

    attempt_start = len(center.telemetry_log)

    center.send_cmd("CMD_CLEAR_CACHE", target_ip=benchlib.BROADCAST_IP)
    time.sleep(args.clear_settle_s)

    seeded = []
    failed_seed = []
    seed_targets = honest_voters if args.seed_honest_only else voters
    for voter in seed_targets:
        if wait_for_seed(center, voter, args.target_domain, args.target_ip, args.seed_attempts, args.seed_timeout_s):
            seeded.append(voter)
        else:
            failed_seed.append(voter)
        time.sleep(args.seed_gap_s)

    print(f"[*] Seeded {len(seeded)}/{len(seed_targets)} requested voters")
    if failed_seed:
        print(f"[!] Seed failed on: {', '.join(failed_seed)}")

    time.sleep(args.settle_s)

    resolve_start = len(center.telemetry_log)
    center.send_cmd("CMD_RESOLVE", arg=args.target_domain, target_ip=resolver)
    result = latest_event(
        center.telemetry_log,
        resolver,
        ["RESOLVE_OK", "RESOLVE_FAIL"],
        resolve_start,
        args.resolve_timeout_s,
    )

    outcome = {
        "attempt": attempt_no,
        "resolver": resolver,
        "voters": voters,
        "byzantine_ip": args.byzantine_ip,
        "seeded": seeded,
        "failed_seed": failed_seed,
        "success": False,
        "event": None,
        "message": "",
        "parsed": {},
        "telemetry_start_index": attempt_start,
        "telemetry_end_index": len(center.telemetry_log),
    }

    if result is None:
        print("[-] No RESOLVE_OK/RESOLVE_FAIL telemetry received")
        outcome["event"] = "TIMEOUT"
        return outcome

    parsed = parse_detail(result["msg"])
    success = result["event"] == "RESOLVE_OK" and parsed.get("source") == "peer_quorum"
    outcome.update({
        "success": success,
        "event": result["event"],
        "message": result["msg"],
        "parsed": parsed,
    })

    if success:
        print(f"[+] BFT peer quorum succeeded: {result['msg']}")
    else:
        print(f"[-] BFT peer quorum failed: {result['msg']}")
        votes = parsed.get("detail_votes")
        quorum = parsed.get("detail_quorum")
        if args.byzantine_ip and votes == quorum:
            print("    Note: votes==quorum but no success often means one Byzantine/wrong vote plus a missing honest vote.")
    return outcome


def write_outputs(center: benchlib.BenchmarkOrchestrator, results_dir: Path, data: Dict[str, Any]) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / "bft_evaluation.json"
    json_path.write_text(json.dumps(data, indent=2))

    telemetry_path = results_dir / "bft_telemetry.csv"
    with telemetry_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["time", "ip", "node_id", "event", "msg"])
        writer.writeheader()
        for entry in center.telemetry_log:
            writer.writerow({key: entry.get(key, "") for key in writer.fieldnames})

    summary_path = results_dir / "bft_summary.csv"
    with summary_path.open("w", newline="") as file:
        fieldnames = [
            "attempt", "success", "event", "resolver", "byzantine_ip",
            "source", "latency_ms", "votes", "quorum", "bft_ms", "message",
        ]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for attempt in data["attempts"]:
            parsed = attempt.get("parsed", {})
            byz = attempt.get("byzantine_ip")
            if byz is None:
                ips = attempt.get("byzantine_ips", [])
                byz = ",".join(ips) if isinstance(ips, list) else (ips or "")
            writer.writerow({
                "attempt": attempt.get("attempt", attempt.get("round", "")),
                "success": attempt.get("success", ""),
                "event": attempt.get("event", ""),
                "resolver": attempt.get("resolver", ""),
                "byzantine_ip": byz,
                "source": parsed.get("source", ""),
                "latency_ms": parsed.get("latency_ms", ""),
                "votes": parsed.get("detail_votes", ""),
                "quorum": parsed.get("detail_quorum", ""),
                "bft_ms": parsed.get("detail_bft_ms", ""),
                "message": attempt.get("message", ""),
            })

    print(f"\n[+] BFT JSON saved to: {json_path}")
    print(f"[+] BFT telemetry saved to: {telemetry_path}")
    print(f"[+] BFT summary saved to: {summary_path}")


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run focused MeshDNS BFT-only benchmarks")
    parser.add_argument("--broadcast", default=os.environ.get("MESHDNS_BROADCAST", benchlib.DEFAULT_BROADCAST_IP))
    parser.add_argument("--telemetry-port", type=int, default=benchlib.DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--command-port", type=int, default=benchlib.DEFAULT_COMMAND_PORT)
    parser.add_argument("--target-domain", default=os.environ.get("MESHDNS_TARGET_DOMAIN", benchlib.DEFAULT_TARGET_DOMAIN))
    parser.add_argument("--target-ip", default=os.environ.get("MESHDNS_TARGET_IP", benchlib.DEFAULT_TARGET_IP))
    parser.add_argument("--nodes", required=True, help="Comma-separated ESP8266 node IPs")
    parser.add_argument("--expected-nodes", type=int, default=0)
    parser.add_argument("--resolver", default="", help="Honest resolver/requester IP")
    parser.add_argument("--byzantine-ip", default="", help="Byzantine node IP, if one node is flashed with BYZANTINE_MODE=1")
    parser.add_argument("--seed-honest-only", action="store_true",
                        help="Do not seed the Byzantine node. Useful to test one-offline tolerance, not fake-vote tolerance.")
    parser.add_argument("--expected-quorum", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--discover-timeout", type=int, default=20)
    parser.add_argument("--clear-settle-s", type=float, default=2.0)
    parser.add_argument("--seed-gap-s", type=float, default=0.5)
    parser.add_argument("--seed-attempts", type=int, default=3)
    parser.add_argument("--seed-timeout-s", type=float, default=3.0)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--resolve-timeout-s", type=float, default=22.0)
    parser.add_argument("--token", default=None)
    parser.add_argument("--config", type=Path, default=repo_root / "firmware" / "meshdns_node" / "config.h")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--run-label", default="bft")
    return parser.parse_args()


def configure_benchlib(args: argparse.Namespace) -> None:
    """Share benchlib globals with bft_benchmark CLI (subset of run_all_benchmarks args)."""
    benchlib.apply_args(args)


def main() -> None:
    args = parse_args()
    nodes = parse_nodes(args.nodes)
    if args.expected_nodes and len(nodes) != args.expected_nodes:
        print(f"[!] --nodes contains {len(nodes)} nodes but --expected-nodes is {args.expected_nodes}")

    configure_benchlib(args)
    center = benchlib.BenchmarkOrchestrator(results_root=args.results_root, run_label=args.run_label)
    center.start_listener()

    try:
        print(f"[*] Waiting up to {args.discover_timeout}s for listed nodes to appear...")
        discovered = center.discover_devices(expected_nodes=len(nodes), timeout_sec=args.discover_timeout)
        missing = sorted(set(nodes) - set(discovered), key=lambda ip: tuple(int(part) for part in ip.split(".")))
        if missing:
            print(f"[!] Listed nodes not seen yet: {', '.join(missing)}")
        print("[*] Starting focused BFT attempts without mDNS/warm/stress phases.")

        attempts = []
        for attempt_no in range(1, args.attempts + 1):
            outcome = run_attempt(center, nodes, args, attempt_no)
            attempts.append(outcome)
            if outcome["success"]:
                break
            if attempt_no < args.attempts:
                print(f"[*] Waiting {args.settle_s:.1f}s before retry...")
                time.sleep(args.settle_s)

        data = {
            "metadata": {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "target_domain": args.target_domain,
                "target_ip": args.target_ip,
                "broadcast": args.broadcast,
                "nodes": nodes,
                "resolver": args.resolver,
                "byzantine_ip": args.byzantine_ip,
                "seed_honest_only": args.seed_honest_only,
            },
            "attempts": attempts,
            "success": any(attempt["success"] for attempt in attempts),
        }
        write_outputs(center, center.results_dir, data)
    finally:
        center.stop()


if __name__ == "__main__":
    main()
