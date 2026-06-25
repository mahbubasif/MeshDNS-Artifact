"""Shared aggregation helpers for adversarial benchmark outputs."""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List


def rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def ci95(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    return 1.96 * statistics.stdev(values) / math.sqrt(len(values))


def summarize_attempts(attempts: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    rows = list(attempts)
    total = len(rows)
    successes = sum(1 for a in rows if a.get("success"))
    timeouts = sum(1 for a in rows if a.get("event") == "TIMEOUT")
    false_accepts = sum(
        1 for a in rows
        if a.get("false_accept")
        or (
            a.get("success")
            and a.get("parsed", {}).get("ip") not in (None, "", a.get("expected_ip"))
        )
    )
    latencies = []
    for a in rows:
        parsed = a.get("parsed", {})
        if a.get("success") and parsed.get("latency_ms"):
            try:
                latencies.append(float(parsed["latency_ms"]))
            except (TypeError, ValueError):
                pass
    return {
        "attempts": total,
        "success_rate": rate(successes, total),
        "timeout_rate": rate(timeouts, total),
        "false_accept_rate": rate(false_accepts, total),
        "median_latency_ms": statistics.median(latencies) if latencies else float("nan"),
        "latency_ci95_ms": ci95(latencies) if latencies else 0.0,
    }


def group_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key, "")), []).append(row)
    return grouped
