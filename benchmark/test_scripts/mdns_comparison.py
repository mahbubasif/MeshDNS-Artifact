#!/usr/bin/env python3
"""
mDNS vs MeshDNS comparison report.

This is intentionally a report/extraction tool, not a second hardware runner.
The canonical artifact path is:

    python3 benchmark/test_scripts/run_all_benchmarks.py ...

That runner measures OS mDNS and MeshDNS in one session and writes
meshdns_evaluation.json plus mdns_meshdns_summary.csv under benchmark/results/.
This script consumes that JSON and regenerates a compact CSV/JSON comparison
for paper tables or plotting.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List


DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[1] / "results"


def latest_evaluation(results_root: Path) -> Path:
    candidates = sorted(
        results_root.glob("*/meshdns_evaluation.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No meshdns_evaluation.json found under {results_root}. "
            "Run benchmark/test_scripts/run_all_benchmarks.py first."
        )
    return candidates[0]


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]


def row_from_result(scenario: str, result: Dict[str, Any]) -> Dict[str, Any]:
    raw = result.get("raw", [])
    if isinstance(raw, list):
        latencies = [float(value) for value in raw if isinstance(value, (int, float))]
    else:
        latencies = []

    avg_ms = result.get("avg_ms", result.get("avg_latency_ms", 0.0))
    if latencies:
        avg_ms = statistics.mean(latencies)

    attempts = result.get("total_sent", len(latencies))
    successes = result.get("successes", len(latencies))
    success_rate = result.get(
        "success_rate",
        (successes / attempts * 100.0) if attempts else 0.0,
    )

    return {
        "scenario": scenario,
        "protocol": result.get("protocol", scenario),
        "attempts": attempts,
        "successes": successes,
        "success_rate": success_rate,
        "mean_ms": avg_ms,
        "median_ms": statistics.median(latencies) if latencies else avg_ms,
        "p95_ms": percentile(latencies, 0.95),
        "qps": result.get("qps", ""),
    }


def build_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    order = [
        ("mdns", "mDNS_OS_Baseline"),
        ("warm", "MeshDNS_Warm_Cache"),
        ("cold_honest", "MeshDNS_Cold_BFT"),
        ("stress_test", "MeshDNS_Stress"),
        ("cold_byzantine", "MeshDNS_Byzantine"),
    ]
    rows = []
    for key, scenario in order:
        result = data.get(key)
        if isinstance(result, dict):
            rows.append(row_from_result(scenario, result))
    return rows


def write_csv(rows: Iterable[Dict[str, Any]], path: Path) -> None:
    rows = list(rows)
    if not rows:
        raise ValueError("No comparison rows to write")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: Iterable[Dict[str, Any]]) -> None:
    print("\n" + "=" * 86)
    print(f"{'scenario':<24}{'protocol':<22}{'n':>6}{'succ%':>8}{'mean':>10}{'median':>10}{'p95':>10}")
    print("=" * 86)
    for row in rows:
        print(
            f"{row['scenario']:<24}{row['protocol']:<22}"
            f"{row['attempts']:>6}{float(row['success_rate']):>7.1f}%"
            f"{float(row['mean_ms']):>10.2f}{float(row['median_ms']):>10.2f}"
            f"{float(row['p95_ms']):>10.2f}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize canonical MeshDNS mDNS comparison results")
    parser.add_argument("--input", type=Path, default=None,
                        help="Path to meshdns_evaluation.json. Defaults to latest under benchmark/results.")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input or latest_evaluation(args.results_root)
    data = json.loads(input_path.read_text())
    rows = build_rows(data)
    print(f"Input: {input_path}")
    print_summary(rows)

    default_stem = input_path.parent / "mdns_meshdns_comparison"
    csv_path = args.csv or default_stem.with_suffix(".csv")
    json_path = args.json or default_stem.with_suffix(".json")
    write_csv(rows, csv_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(rows, indent=2))
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {json_path}")


if __name__ == "__main__":
    main()
