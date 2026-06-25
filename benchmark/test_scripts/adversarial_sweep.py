#!/usr/bin/env python3
"""
Hardware adversarial matrix: sweep f=0..max_f with flash checklist.

Each f level spawns adversarial_benchmark.py with full inter-round timing flags.
Use --interactive on the sweep only (child benchmark does not ask ENTER again).

Example:
  export MESHDNS_BROADCAST=192.168.1.255
  export MESHDNS_TARGET_DOMAIN=lab-target.local
  export MESHDNS_TARGET_IP=192.168.1.10
  export MESHDNS_NODES=192.168.1.21,192.168.1.22,192.168.1.23,192.168.1.24,192.168.1.25
  export MESHDNS_RESOLVER=192.168.1.22
  export MESHDNS_BYZANTINE_POOL=192.168.1.21,192.168.1.23,192.168.1.24
  python3 benchmark/test_scripts/adversarial_sweep.py \\
    --nodes $MESHDNS_NODES \\
    --byzantine-pool $MESHDNS_BYZANTINE_POOL \\
    --resolver $MESHDNS_RESOLVER \\
    --broadcast $MESHDNS_BROADCAST \\
    --target-domain $MESHDNS_TARGET_DOMAIN \\
    --target-ip $MESHDNS_TARGET_IP \\
    --max-f 2 --rounds 5 --interactive
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parents[2]
_BENCH = _REPO / "benchmark" / "test_scripts"
_RESULTS = _REPO / "benchmark" / "results"
if str(_BENCH.parent) not in sys.path:
    sys.path.insert(0, str(_BENCH.parent))


def parse_nodes(value: str) -> List[str]:
    return sorted(
        {ip.strip() for ip in value.split(",") if ip.strip()},
        key=lambda ip: tuple(int(p) for p in ip.split(".")),
    )


def honest_voter_count(nodes: List[str], resolver: str, byz: List[str]) -> int:
    byz_set = set(byz)
    return sum(1 for n in nodes if n != resolver and n not in byz_set)


def flash_checklist(
    f: int,
    byz_pool: List[str],
    all_nodes: List[str],
    resolver: str,
) -> str:
    byz = byz_pool[:f]
    honest_n = honest_voter_count(all_nodes, resolver, byz)
    lines = [
        f"\n{'=' * 72}",
        f"FLASH CHECKLIST — f={f}",
        f"{'=' * 72}",
        "config.h on each node:",
        "  #define BYZANTINE_MODE 0   (honest)",
        "  #define BYZANTINE_MODE 1   (adversary)",
        "  #define BYZANTINE_LEVEL 1",
        "  #define BYZANTINE_SYBIL_EXTRA_KEYS 0",
        "",
        f"Resolver (stay honest): {resolver or '(auto)'}",
        f"Honest voters available for seeding: {honest_n} (need >=3 for quorum on this mesh)",
        "",
    ]
    if honest_n < 3:
        lines.append(
            "[!] WARNING: fewer than 3 honest voters — peer_quorum will likely fail "
            "(expected for f=2 on a 5-node testbed). Report false_accept, not success_rate."
        )
        lines.append("")
    byz_set = set(byz)
    for ip in all_nodes:
        if ip in byz_set:
            lines.append(f"  {ip}: BYZANTINE_MODE=1, LEVEL=1, SYBIL_EXTRA_KEYS=0")
        else:
            lines.append(f"  {ip}: BYZANTINE_MODE=0")
    lines.append("")
    lines.append("Wait ~30s after flashing so discovery shows peers>=3, then press ENTER.")
    lines.append("")
    return "\n".join(lines)


def latest_result_dir(label: str) -> Optional[Path]:
    if not _RESULTS.is_dir():
        return None
    matches = sorted(_RESULTS.glob(f"{label}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def load_run_metrics(label: str) -> Dict[str, Any]:
    folder = latest_result_dir(label)
    if not folder:
        return {}
    json_path = folder / "adversarial_hardware.json"
    if not json_path.is_file():
        return {"result_dir": str(folder)}
    data = json.loads(json_path.read_text())
    scenarios = data.get("scenarios", [])
    if not scenarios:
        return {"result_dir": str(folder)}
    summary = scenarios[0].get("summary", {})
    return {
        "result_dir": str(folder),
        "success_rate": summary.get("success_rate"),
        "timeout_rate": summary.get("timeout_rate"),
        "false_accept_rate": summary.get("false_accept_rate"),
    }


def run_benchmark(args: argparse.Namespace, byzantine_ips: List[str], label: str) -> int:
    cmd = [
        sys.executable,
        str(_BENCH / "adversarial_benchmark.py"),
        "--nodes", args.nodes,
        "--byzantine-ips", ",".join(byzantine_ips),
        "--rounds", str(args.rounds),
        "--label", label,
        "--broadcast", args.broadcast,
        "--target-domain", args.target_domain,
        "--target-ip", args.target_ip,
        "--resolver", args.resolver,
        "--resolve-timeout-s", str(args.resolve_timeout_s),
        "--round-gap-s", str(args.round_gap_s),
        "--clear-settle-s", str(args.clear_settle_s),
        "--clear-ack-timeout-s", str(args.clear_ack_timeout_s),
        "--resolver-idle-timeout-s", str(args.resolver_idle_timeout_s),
        "--resolver-idle-stable-s", str(args.resolver_idle_stable_s),
        "--pre-seed-pause-s", str(args.pre_seed_pause_s),
        "--settle-s", str(args.settle_s),
        "--seed-gap-s", str(args.seed_gap_s),
        "--seed-timeout-s", str(args.seed_timeout_s),
        "--seed-attempts", str(args.seed_attempts),
        "--discover-timeout", str(args.discover_timeout),
        "--fleet-stable-sec", str(args.fleet_stable_sec),
        "--fleet-stable-timeout-s", str(args.fleet_stable_timeout_s),
        "--fleet-min-peers", str(args.fleet_min_peers),
        "--expected-quorum", str(args.expected_quorum),
    ]
    if args.seed_honest_only:
        cmd.append("--seed-honest-only")
    # Parent sweep already waited for flash; do not double-prompt in child.
    print(f"\n[*] Running: {' '.join(cmd)}\n")
    return subprocess.call(cmd, cwd=str(_REPO))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep adversarial f on hardware testbed")
    parser.add_argument("--nodes", required=True)
    parser.add_argument(
        "--byzantine-pool",
        required=True,
        help="Ordered IPs to flash as Byzantine for f=1,2,... (first k used at each f)",
    )
    parser.add_argument("--resolver", default=os.environ.get("MESHDNS_RESOLVER", ""))
    parser.add_argument("--max-f", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--broadcast", default=os.environ.get("MESHDNS_BROADCAST", "192.168.1.255"))
    parser.add_argument("--target-domain", default=os.environ.get("MESHDNS_TARGET_DOMAIN", "lab-target.local"))
    parser.add_argument("--target-ip", default=os.environ.get("MESHDNS_TARGET_IP", "192.168.1.10"))
    parser.add_argument("--expected-quorum", type=int, default=3)
    parser.add_argument("--resolve-timeout-s", type=float, default=22.0)
    parser.add_argument("--round-gap-s", type=float, default=10.0)
    parser.add_argument("--clear-settle-s", type=float, default=3.0)
    parser.add_argument("--clear-ack-timeout-s", type=float, default=10.0)
    parser.add_argument("--resolver-idle-timeout-s", type=float, default=15.0)
    parser.add_argument("--resolver-idle-stable-s", type=float, default=2.0)
    parser.add_argument("--pre-seed-pause-s", type=float, default=3.0)
    parser.add_argument("--settle-s", type=float, default=3.0)
    parser.add_argument("--seed-gap-s", type=float, default=0.75)
    parser.add_argument("--seed-attempts", type=int, default=3)
    parser.add_argument("--seed-timeout-s", type=float, default=4.0)
    parser.add_argument("--discover-timeout", type=int, default=25)
    parser.add_argument("--fleet-stable-sec", type=float, default=6.0)
    parser.add_argument("--fleet-stable-timeout-s", type=float, default=60.0)
    parser.add_argument("--fleet-min-peers", type=int, default=3)
    parser.add_argument("--seed-honest-only", action="store_true", default=True)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print checklist only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.resolver:
        print("[!] --resolver (or MESHDNS_RESOLVER) is required for consistent cold-query tests")
        return 2

    nodes = parse_nodes(args.nodes)
    pool = parse_nodes(args.byzantine_pool)
    max_f = min(args.max_f, (len(nodes) - 1) // 2, len(pool))

    print(f"[*] Sweep: n={len(nodes)}, max_f={max_f}, rounds={args.rounds}, pool={pool}")
    print(f"[*] Timing: round_gap={args.round_gap_s}s, resolve_timeout={args.resolve_timeout_s}s")

    summary: List[Dict[str, Any]] = []
    for f in range(0, max_f + 1):
        byz = pool[:f]
        label = f"adversarial_f{f}"
        print(flash_checklist(f, pool, nodes, args.resolver))
        if args.dry_run:
            summary.append({"f": f, "byzantine_ips": byz, "status": "dry_run"})
            continue
        if args.interactive:
            input(f"Press ENTER after flashing f={f} configuration...")
        rc = run_benchmark(args, byz, label=label)
        metrics = load_run_metrics(label)
        entry = {
            "f": f,
            "byzantine_ips": byz,
            "exit_code": rc,
            **metrics,
        }
        summary.append(entry)
        if metrics:
            print(
                f"[+] f={f}: success={metrics.get('success_rate')}, "
                f"timeout={metrics.get('timeout_rate')}, "
                f"false_accept={metrics.get('false_accept_rate')}"
            )

    out = _RESULTS / "adversarial_sweep_summary.json"
    out.write_text(json.dumps({"summary": summary}, indent=2))
    print(f"\n[+] Sweep summary: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
