#!/usr/bin/env python3
"""
Run MeshDNS adversarial evaluation sweeps (simulation).

Examples:
  python3 sim/adversarial_evaluation.py --suite vary-f --n 7 10 20 --rounds 100
  python3 sim/adversarial_evaluation.py --suite collusion --n 7 --rounds 200
  python3 sim/adversarial_evaluation.py --suite sybil --n 4 --rounds 100
  python3 sim/adversarial_evaluation.py --suite quorum-failure --rounds 100
  python3 sim/adversarial_evaluation.py --suite equivocation --rounds 100
  python3 sim/adversarial_evaluation.py --suite trust --n 5 --rounds 100
  python3 sim/adversarial_evaluation.py --suite all --quick
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from adversarial_lib import (
    ByzantineLevel,
    build_mesh,
    configure_cache,
    min_sybil_to_break,
    run_monte_carlo,
    sweep_varying_f,
    trust_convergence_rounds,
)


DEFAULT_OUT = Path(__file__).resolve().parents[1] / "benchmark" / "results"


def suite_vary_f(n_values: List[int], rounds: int, seed: int, packet_loss: float) -> Dict[str, Any]:
    rows = []
    for n in n_values:
        for level in (ByzantineLevel.L1_FIXED, ByzantineLevel.L5_COLLUDE, ByzantineLevel.CRASH):
            part = sweep_varying_f(n, rounds, level=level, packet_loss=packet_loss, seed=seed)
            rows.extend(part)
    return {"test": "vary_f", "rows": rows}


def suite_collusion(n: int, rounds: int, seed: int) -> Dict[str, Any]:
    rows = []
    for f in (1, 2, 3):
        if f > (n - 1) // 2:
            continue
        voters = build_mesh(n, f, level=ByzantineLevel.L5_COLLUDE)
        configure_cache(voters, distributed_miss=True)
        metrics = run_monte_carlo(voters, rounds, seed=seed)
        metrics.update({"n": n, "f": f, "scenario": "distributed_miss_collusion"})
        rows.append(metrics)
        voters2 = build_mesh(n, f, level=ByzantineLevel.L5_COLLUDE)
        configure_cache(voters2, seed_honest=True, seed_byzantine=False)
        metrics2 = run_monte_carlo(voters2, rounds, seed=seed + 1)
        metrics2.update({"n": n, "f": f, "scenario": "honest_seeded_collusion"})
        rows.append(metrics2)
    return {"test": "collusion", "rows": rows}


def suite_sybil(n_physical: int, rounds: int, seed: int) -> Dict[str, Any]:
    rows = []
    for k in range(0, 6):
        voters = build_mesh(n_physical, 1, level=ByzantineLevel.L5_COLLUDE, sybil_per_physical=k)
        configure_cache(voters, distributed_miss=True)
        metrics = run_monte_carlo(voters, rounds, seed=seed + k)
        metrics.update({"n_physical": n_physical, "sybil_extra_keys": k})
        rows.append(metrics)
    summary = min_sybil_to_break(n_physical, n_physical - 1, rounds=min(rounds, 100), seed=seed)
    return {"test": "sybil", "rows": rows, "summary": summary}


def suite_quorum_failure(rounds: int, seed: int) -> Dict[str, Any]:
    matrix = [
        ("benign_0_loss", 7, 0, ByzantineLevel.HONEST, 0.0),
        ("benign_10_loss", 7, 0, ByzantineLevel.HONEST, 0.10),
        ("crash_f2", 7, 2, ByzantineLevel.CRASH, 0.0),
        ("slow_f2", 7, 2, ByzantineLevel.SLOW, 0.0),
        ("garbage_f2", 7, 2, ByzantineLevel.GARBAGE, 0.0),
        ("l1_f1", 7, 1, ByzantineLevel.L1_FIXED, 0.05),
    ]
    rows = []
    for label, n, f, level, loss in matrix:
        voters = build_mesh(n, f, level=level if f else ByzantineLevel.HONEST)
        configure_cache(voters, seed_honest=True, seed_byzantine=False)
        metrics = run_monte_carlo(voters, rounds, packet_loss=loss, seed=seed)
        metrics.update({"label": label, "n": n, "f": f, "packet_loss": loss})
        rows.append(metrics)
    return {"test": "quorum_failure", "rows": rows}


def suite_equivocation(rounds: int, seed: int) -> Dict[str, Any]:
    voters = build_mesh(5, 1, level=ByzantineLevel.L4_EQUIVOCATE)
    configure_cache(voters, seed_honest=True, seed_byzantine=True)
    metrics = run_monte_carlo(voters, rounds, seed=seed)
    metrics.update({"n": 5, "f": 1, "level": ByzantineLevel.L4_EQUIVOCATE.value})
    return {"test": "equivocation", "rows": [metrics]}


def suite_trust(n: int, rounds: int, seed: int) -> Dict[str, Any]:
    result = trust_convergence_rounds(n, rounds=rounds, seed=seed)
    return {"test": "trust_convergence", "result": result}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MeshDNS adversarial simulation evaluation")
    parser.add_argument(
        "--suite",
        choices=["vary-f", "collusion", "sybil", "quorum-failure", "equivocation", "trust", "all"],
        default="all",
    )
    parser.add_argument("--n", type=int, nargs="+", default=[5, 7, 10, 20, 50])
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--packet-loss", type=float, default=0.0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--quick", action="store_true", help="Smaller n list and 30 rounds")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.quick:
        args.n = [5, 7, 10]
        args.rounds = min(args.rounds, 30)

    suites: List[Dict[str, Any]] = []
    if args.suite in ("vary-f", "all"):
        suites.append(suite_vary_f(args.n, args.rounds, args.seed, args.packet_loss))
    if args.suite in ("collusion", "all"):
        suites.append(suite_collusion(7, args.rounds, args.seed))
    if args.suite in ("sybil", "all"):
        for n_phys in (4, 5, 7):
            suites.append(suite_sybil(n_phys, args.rounds, args.seed))
    if args.suite in ("quorum-failure", "all"):
        suites.append(suite_quorum_failure(args.rounds, args.seed))
    if args.suite in ("equivocation", "all"):
        suites.append(suite_equivocation(args.rounds, args.seed))
    if args.suite in ("trust", "all"):
        suites.append(suite_trust(5, args.rounds, args.seed))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"adversarial_sim_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "suite": args.suite,
            "rounds": args.rounds,
            "seed": args.seed,
            "packet_loss": args.packet_loss,
            "n_values": args.n,
        },
        "suites": suites,
    }
    out_path = out_dir / "adversarial_simulation.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[+] Wrote {out_path}")
    for suite in suites:
        print(f"\n=== {suite['test']} ===")
        if "rows" in suite:
            for row in suite["rows"][:8]:
                keys = ("n", "f", "label", "scenario", "success_rate", "timeout_rate", "false_accept_rate")
                brief = {k: row[k] for k in keys if k in row}
                print(f"  {brief}")
            if len(suite["rows"]) > 8:
                print(f"  ... {len(suite['rows']) - 8} more rows")
        if "summary" in suite:
            print(f"  summary: {suite['summary']}")
        if "result" in suite:
            r = suite["result"]
            print(f"  byzantine trust final={r.get('final_byzantine_trust'):.3f}, "
                  f"rounds_to_threshold={r.get('rounds_to_trust_below')}")


if __name__ == "__main__":
    main()
