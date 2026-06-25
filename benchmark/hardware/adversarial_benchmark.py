#!/usr/bin/env python3
"""
Systematic hardware adversarial benchmarks for the ESP8266 testbed.

Requires BYZANTINE_MODE=1 on nodes listed in --byzantine-ips or in the scenario file.
See benchmark/ADVERSARIAL_EVAL.md for the full matrix and flash workflow.

Examples:
  export MESHDNS_NODES=192.168.1.21,192.168.1.22,192.168.1.23,192.168.1.24,192.168.1.25
  export MESHDNS_RESOLVER=192.168.1.22
  export MESHDNS_BYZANTINE_1=192.168.1.21
  python3 benchmark/hardware/adversarial_benchmark.py \\
    --nodes "$MESHDNS_NODES" --resolver "$MESHDNS_RESOLVER" \\
    --byzantine-ips "$MESHDNS_BYZANTINE_1" --rounds 10 --label hw_f1

  python3 benchmark/hardware/adversarial_benchmark.py \\
    --nodes ... --scenarios benchmark/adversarial_scenarios.json --interactive
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

_BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))

import run_all_benchmarks as benchlib
from bft_benchmark import (
    choose_resolver,
    configure_benchlib,
    parse_detail,
    parse_nodes,
    wait_for_seed,
)
from adversarial_metrics import summarize_attempts

DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[1] / "results"
DEFAULT_SCENARIOS = Path(__file__).resolve().parents[1] / "adversarial_scenarios.json"


def wait_for_all_nodes_event(
    center: benchlib.BenchmarkOrchestrator,
    nodes: List[str],
    event: str,
    start_index: int,
    timeout_s: float,
) -> tuple[bool, set[str]]:
    """Wait until every listed node has emitted `event` since start_index."""
    needed = set(nodes)
    seen: set[str] = set()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for entry in center.telemetry_log[start_index:]:
            if entry.get("event") == event and entry["ip"] in needed:
                seen.add(entry["ip"])
        if seen >= needed:
            return True, seen
        time.sleep(0.05)
    return False, seen


def wait_for_resolver_idle(
    center: benchlib.BenchmarkOrchestrator,
    resolver: str,
    timeout_s: float,
    stable_s: float = 1.5,
) -> bool:
    """Wait until resolver heartbeats show queries=0 (no in-flight CMD_RESOLVE)."""
    deadline = time.time() + timeout_s
    idle_since: Optional[float] = None
    while time.time() < deadline:
        hb = center.latest_heartbeat_by_ip().get(resolver)
        if hb:
            fields = hb.get("fields", {})
            try:
                queries = int(fields.get("queries", "0"))
            except ValueError:
                queries = 0
            if queries == 0:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since >= stable_s:
                    return True
            else:
                idle_since = None
        time.sleep(0.1)
    return False


def effective_min_seeded(args: argparse.Namespace, seed_targets: List[str]) -> int:
    """Votes needed for quorum — not every voter must be seeded."""
    if args.min_seeded_voters > 0:
        return min(args.min_seeded_voters, len(seed_targets))
    return min(args.expected_quorum, len(seed_targets))


def seed_honest_voters(
    center: benchlib.BenchmarkOrchestrator,
    seed_targets: List[str],
    args: argparse.Namespace,
) -> tuple[List[str], List[str]]:
    """Seed until quorum count is met; skip slow stragglers."""
    min_seed = effective_min_seeded(args, seed_targets)
    seeded: List[str] = []
    failed_seed: List[str] = []
    skipped: List[str] = []

    print(f"[*] Seeding voters (need >={min_seed} for quorum={args.expected_quorum}, "
          f"targets={len(seed_targets)})")

    for voter in seed_targets:
        if len(seeded) >= min_seed:
            skipped.append(voter)
            continue
        if wait_for_seed(
            center, voter, args.target_domain, args.target_ip,
            args.seed_attempts, args.seed_timeout_s,
        ):
            seeded.append(voter)
        else:
            failed_seed.append(voter)
        time.sleep(args.seed_gap_s)

    if skipped:
        print(f"[*] Quorum seeds ready ({len(seeded)}>={min_seed}); skipped: {', '.join(skipped)}")
    if failed_seed and len(seeded) >= min_seed:
        print(f"[!] Seed failed on {', '.join(failed_seed)} but {len(seeded)} seeds suffice for quorum")
    elif failed_seed:
        print(f"[!] Seed failed on: {', '.join(failed_seed)}")

    return seeded, failed_seed


def inter_round_pause(
    center: benchlib.BenchmarkOrchestrator,
    nodes: List[str],
    resolver: str,
    args: argparse.Namespace,
    round_no: int,
) -> None:
    """Let ESP8266 finish voting/crypto before the next clear+seed cycle."""
    if round_no <= 1:
        return
    print(f"[*] Inter-round recovery after round {round_no - 1}...")
    if not wait_for_resolver_idle(center, resolver, args.resolver_idle_timeout_s, args.resolver_idle_stable_s):
        print(f"[!] Resolver {resolver} still busy (queries>0); continuing after wait")
    time.sleep(args.round_gap_s)


def run_round(
    center: benchlib.BenchmarkOrchestrator,
    nodes: List[str],
    args: argparse.Namespace,
    round_no: int,
    byzantine_ips: List[str],
    seed_honest_only: bool,
    seed_byzantine_only: bool,
) -> Dict[str, Any]:
    byz_set = set(byzantine_ips)
    resolver = choose_resolver(nodes, args.resolver, byzantine_ips[0] if byzantine_ips else "")
    voters = [n for n in nodes if n != resolver]
    honest_voters = [n for n in voters if n not in byz_set]

    attempt_start = len(center.telemetry_log)
    if seed_byzantine_only:
        seed_targets = [n for n in voters if n in byz_set]
    elif seed_honest_only:
        seed_targets = honest_voters
    else:
        seed_targets = voters

    min_seed = effective_min_seeded(args, seed_targets)
    must_clear = sorted(set(seed_targets) | {resolver})

    clear_start = len(center.telemetry_log)
    center.send_cmd("CMD_CLEAR_CACHE", target_ip=benchlib.BROADCAST_IP)
    cleared_ok, cleared_ips = wait_for_all_nodes_event(
        center, must_clear, "CACHE_CLEARED", clear_start, args.clear_ack_timeout_s,
    )
    if cleared_ok:
        print(f"[*] Cache cleared on resolver + {len(seed_targets)} voter(s)")
    else:
        missing = sorted(set(must_clear) - cleared_ips)
        print(f"[!] CACHE_CLEARED missing from: {', '.join(missing)} (continuing)")
    time.sleep(args.clear_settle_s)

    if not wait_for_resolver_idle(center, resolver, args.resolver_idle_timeout_s, args.resolver_idle_stable_s):
        print(f"[!] Resolver {resolver} not idle before seed; extra {args.pre_seed_pause_s:.0f}s")
        time.sleep(args.pre_seed_pause_s)

    seeded, failed_seed = seed_honest_voters(center, seed_targets, args)

    if len(seeded) < min_seed:
        print(f"[-] Round {round_no}: only {len(seeded)}/{min_seed} voters seeded — skip resolve")
        return {
            "round": round_no,
            "resolver": resolver,
            "byzantine_ips": byzantine_ips,
            "seeded": seeded,
            "failed_seed": failed_seed,
            "success": False,
            "false_accept": False,
            "event": "SEED_INSUFFICIENT",
            "message": f"seeded={len(seeded)},need={min_seed}",
            "parsed": {},
            "expected_ip": args.target_ip,
            "equivoc_events": 0,
            "trust_min": "",
            "telemetry_start_index": attempt_start,
            "telemetry_end_index": len(center.telemetry_log),
        }

    time.sleep(args.settle_s)
    resolve_start = len(center.telemetry_log)
    center.send_cmd("CMD_RESOLVE", arg=args.target_domain, target_ip=resolver)

    deadline = time.time() + args.resolve_timeout_s
    result = None
    while time.time() < deadline:
        for idx in range(resolve_start, len(center.telemetry_log)):
            entry = center.telemetry_log[idx]
            if entry["ip"] == resolver and entry["event"] in ("RESOLVE_OK", "RESOLVE_FAIL"):
                result = entry
                break
        if result:
            break
        time.sleep(0.05)

    parsed = parse_detail(result["msg"]) if result else {}
    accepted_ip = parsed.get("ip", "")
    success = bool(result and result["event"] == "RESOLVE_OK" and parsed.get("source") == "peer_quorum")
    false_accept = success and accepted_ip and accepted_ip != args.target_ip

    equivoc_events = sum(
        1 for entry in center.telemetry_log[attempt_start:]
        if entry.get("event") == "EQUIVOC_DETECTED"
    )

    if success:
        print(f"[+] Round {round_no}: peer_quorum OK — {result['msg']}")
    elif false_accept:
        print(f"[!] Round {round_no}: FALSE ACCEPT (wrong IP) — {result['msg']}")
    elif result:
        print(f"[-] Round {round_no}: {result['event']} — {result['msg']}")
    else:
        print(f"[-] Round {round_no}: TIMEOUT (no RESOLVE_OK/FAIL within {args.resolve_timeout_s}s)")

    return {
        "round": round_no,
        "resolver": resolver,
        "byzantine_ips": byzantine_ips,
        "seeded": seeded,
        "failed_seed": failed_seed,
        "success": success,
        "false_accept": false_accept,
        "event": result["event"] if result else "TIMEOUT",
        "message": result["msg"] if result else "",
        "parsed": parsed,
        "expected_ip": args.target_ip,
        "equivoc_events": equivoc_events,
        "trust_min": parsed.get("detail_trust_min", parsed.get("trust_min", "")),
        "telemetry_start_index": attempt_start,
        "telemetry_end_index": len(center.telemetry_log),
    }


def write_adversarial_outputs(
    center: benchlib.BenchmarkOrchestrator,
    results_dir: Path,
    payload: Dict[str, Any],
) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)

    telemetry_path = results_dir / "adversarial_telemetry.csv"
    with telemetry_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["time", "ip", "node_id", "event", "msg"])
        writer.writeheader()
        for entry in center.telemetry_log:
            writer.writerow({key: entry.get(key, "") for key in writer.fieldnames})

    summary_path = results_dir / "adversarial_summary.csv"
    fieldnames = [
        "scenario_id", "round", "success", "false_accept", "event", "resolver",
        "byzantine_ips", "source", "latency_ms", "votes", "quorum", "bft_ms",
        "equivoc_events", "trust_min", "seeded_count", "message",
    ]
    with summary_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for scenario_result in payload.get("scenarios", []):
            sid = scenario_result.get("summary", {}).get("scenario_id", "")
            for attempt in scenario_result.get("attempts", []):
                parsed = attempt.get("parsed", {})
                byz = attempt.get("byzantine_ips", [])
                writer.writerow({
                    "scenario_id": sid,
                    "round": attempt.get("round", ""),
                    "success": attempt.get("success", ""),
                    "false_accept": attempt.get("false_accept", ""),
                    "event": attempt.get("event", ""),
                    "resolver": attempt.get("resolver", ""),
                    "byzantine_ips": ",".join(byz) if isinstance(byz, list) else byz,
                    "source": parsed.get("source", ""),
                    "latency_ms": parsed.get("latency_ms", ""),
                    "votes": parsed.get("detail_votes", ""),
                    "quorum": parsed.get("detail_quorum", ""),
                    "bft_ms": parsed.get("detail_bft_ms", ""),
                    "equivoc_events": attempt.get("equivoc_events", ""),
                    "trust_min": attempt.get("trust_min", ""),
                    "seeded_count": len(attempt.get("seeded", [])),
                    "message": attempt.get("message", ""),
                })

    print(f"\n[+] Telemetry CSV: {telemetry_path}")
    print(f"[+] Summary CSV: {summary_path}")


def run_scenario(
    center: benchlib.BenchmarkOrchestrator,
    nodes: List[str],
    args: argparse.Namespace,
    scenario: Dict[str, Any],
) -> Dict[str, Any]:
    raw_byz = [str(ip).strip() for ip in scenario.get("byzantine_ips", []) if str(ip).strip()]
    placeholder_byz = [ip for ip in raw_byz if ip.startswith("REPLACE_WITH_")]
    if placeholder_byz:
        raise ValueError(
            f"Scenario {scenario.get('id')} still contains placeholder Byzantine IPs: "
            f"{', '.join(placeholder_byz)}. Edit benchmark/adversarial_scenarios.json "
            "or pass --byzantine-ips directly."
        )
    byz = raw_byz

    print("\n" + "=" * 72)
    print(f"SCENARIO: {scenario.get('label', scenario.get('id'))}")
    print("=" * 72)
    if byz:
        print(f"Byzantine IPs (flash BYZANTINE_MODE=1): {', '.join(byz)}")
    else:
        print("Byzantine IPs: none (all honest)")
    if scenario.get("notes"):
        print(f"Notes: {scenario['notes']}")
    if args.interactive:
        input("Press ENTER when firmware is flashed and nodes are online...")

    rounds = scenario.get("rounds", args.rounds)
    seed_byzantine_only = scenario.get("seed_byzantine_only", False)
    seed_honest_only = scenario.get("seed_honest_only", args.seed_honest_only)
    if seed_byzantine_only:
        seed_honest_only = False

    attempts = []
    resolver = choose_resolver(nodes, args.resolver, byz[0] if byz else "")
    for r in range(1, rounds + 1):
        print(f"\n[*] Round {r}/{rounds}")
        inter_round_pause(center, nodes, resolver, args, r)
        attempts.append(
            run_round(
                center, nodes, args, r, byz,
                seed_honest_only=seed_honest_only,
                seed_byzantine_only=seed_byzantine_only,
            )
        )

    summary = summarize_attempts(attempts)
    summary["scenario_id"] = scenario.get("id")
    summary["f"] = len(byz)
    summary["n_nodes"] = len(nodes)
    return {
        "scenario": scenario,
        "attempts": attempts,
        "summary": summary,
    }


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="MeshDNS systematic hardware adversarial benchmarks")
    parser.add_argument("--nodes", required=True, help="Comma-separated node IPs")
    parser.add_argument("--byzantine-ips", default="", help="Comma-separated Byzantine node IPs for single-scenario run")
    parser.add_argument("--scenarios", type=Path, default=None, help="JSON scenario file")
    parser.add_argument("--scenario-id", default="", help="Run only this scenario id from JSON")
    parser.add_argument("--interactive", action="store_true", help="Pause before each scenario for re-flash")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--label", default="adversarial_hw")
    parser.add_argument("--broadcast", default=os.environ.get("MESHDNS_BROADCAST", benchlib.DEFAULT_BROADCAST_IP))
    parser.add_argument("--telemetry-port", type=int, default=benchlib.DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--command-port", type=int, default=benchlib.DEFAULT_COMMAND_PORT)
    parser.add_argument("--target-domain", default=os.environ.get("MESHDNS_TARGET_DOMAIN", benchlib.DEFAULT_TARGET_DOMAIN))
    parser.add_argument("--target-ip", default=os.environ.get("MESHDNS_TARGET_IP", benchlib.DEFAULT_TARGET_IP))
    parser.add_argument("--resolver", default="")
    parser.add_argument("--seed-honest-only", action="store_true", default=True)
    parser.add_argument("--seed-byzantine-only", action="store_true")
    parser.add_argument("--expected-quorum", type=int, default=3)
    parser.add_argument("--discover-timeout", type=int, default=25)
    parser.add_argument("--fleet-stable-sec", type=float, default=8.0,
                        help="Seconds all nodes must show fresh heartbeats before rounds")
    parser.add_argument("--fleet-stable-timeout-s", type=float, default=45.0)
    parser.add_argument("--fleet-min-peers", type=int, default=0,
                        help="Min peers in heartbeat (0 = auto: max(0, n-2))")
    parser.add_argument("--clear-settle-s", type=float, default=3.0,
                        help="Sleep after all nodes ack CACHE_CLEARED")
    parser.add_argument("--clear-ack-timeout-s", type=float, default=8.0,
                        help="Wait for CACHE_CLEARED from resolver + voters that will be seeded")
    parser.add_argument("--min-seeded-voters", type=int, default=0,
                        help="Stop seeding once this many succeed (0 = use expected-quorum)")
    parser.add_argument("--round-gap-s", type=float, default=8.0,
                        help="Pause between rounds so resolver finishes prior vote")
    parser.add_argument("--resolver-idle-timeout-s", type=float, default=12.0,
                        help="Max wait for resolver heartbeat queries=0")
    parser.add_argument("--resolver-idle-stable-s", type=float, default=1.5,
                        help="Resolver must show queries=0 for this long")
    parser.add_argument("--pre-seed-pause-s", type=float, default=3.0,
                        help="Extra pause if resolver still busy before seeding")
    parser.add_argument("--seed-gap-s", type=float, default=0.75)
    parser.add_argument("--seed-attempts", type=int, default=3)
    parser.add_argument("--seed-timeout-s", type=float, default=4.0)
    parser.add_argument("--settle-s", type=float, default=3.0,
                        help="Pause after seeding, before CMD_RESOLVE")
    parser.add_argument("--resolve-timeout-s", type=float, default=22.0)
    parser.add_argument("--token", default=None)
    parser.add_argument("--config", type=Path, default=repo_root / "firmware" / "meshdns_node" / "config.h")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    return parser.parse_args()


def load_scenarios(path: Path, scenario_id: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    data = json.loads(path.read_text())
    scenarios = data.get("scenarios", [])
    if scenario_id:
        scenarios = [s for s in scenarios if s.get("id") == scenario_id]
    return scenarios, data.get("defaults", {})


def main() -> None:
    args = parse_args()
    nodes = parse_nodes(args.nodes)
    configure_benchlib(args)

    if args.scenarios:
        scenarios, defaults = load_scenarios(args.scenarios, args.scenario_id)
        if defaults.get("broadcast"):
            args.broadcast = defaults["broadcast"]
        if defaults.get("target_ip"):
            args.target_ip = defaults["target_ip"]
        if defaults.get("target_domain"):
            args.target_domain = defaults["target_domain"]
        if defaults.get("rounds"):
            args.rounds = int(defaults["rounds"])
    else:
        byz = [ip.strip() for ip in args.byzantine_ips.split(",") if ip.strip()]
        scenarios = [{
            "id": args.label,
            "label": args.label,
            "byzantine_ips": byz,
            "rounds": args.rounds,
            "seed_honest_only": args.seed_honest_only,
            "seed_byzantine_only": args.seed_byzantine_only,
        }]

    center = benchlib.BenchmarkOrchestrator(results_root=args.results_root, run_label=args.label)
    center.start_listener()
    try:
        print(f"[*] Discovering {len(nodes)} nodes...")
        center.discover_devices(expected_nodes=len(nodes), timeout_sec=args.discover_timeout)
        fleet_min = args.fleet_min_peers if args.fleet_min_peers > 0 else None
        center.wait_for_stable_fleet(
            nodes,
            stable_sec=args.fleet_stable_sec,
            min_peers=fleet_min,
            timeout_sec=args.fleet_stable_timeout_s,
        )

        results = []
        for scenario in scenarios:
            results.append(run_scenario(center, nodes, args, scenario))

        payload = {
            "metadata": {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "nodes": nodes,
                "target_domain": args.target_domain,
                "target_ip": args.target_ip,
            },
            "scenarios": results,
        }
        out_json = center.results_dir / "adversarial_hardware.json"
        out_json.write_text(json.dumps(payload, indent=2))
        write_adversarial_outputs(center, center.results_dir, payload)

        print("\n" + "=" * 72)
        print("SUMMARY")
        print("=" * 72)
        for r in results:
            s = r["summary"]
            print(
                f"  {s.get('scenario_id')}: f={s.get('f')} "
                f"success={s['success_rate']:.1%} timeout={s['timeout_rate']:.1%} "
                f"false_accept={s['false_accept_rate']:.1%}"
            )
        print(f"\n[+] Full report: {out_json}")
    finally:
        center.stop()


if __name__ == "__main__":
    main()
