#!/usr/bin/env python3
"""
Plot INA219 power-over-time from energy_profile_benchmark results.

Example:
  python3 benchmark/hardware/plot_energy_power_timeline.py \
    benchmark/results/energy_20260522_031814
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif"],
    "font.size": 10,
    "axes.labelsize": 10,
    "axes.titlesize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 8,
    "figure.dpi": 300,
    "lines.linewidth": 1.2,
    "grid.alpha": 0.45,
    "grid.linestyle": "--",
})


def load_run(run_dir: Path) -> tuple[list[float], list[float], dict]:
    csv_path = run_dir / "ina219_samples.csv"
    json_path = run_dir / "energy_profile.json"
    if not csv_path.exists() or not json_path.exists():
        raise FileNotFoundError(f"Need ina219_samples.csv and energy_profile.json in {run_dir}")

    times: list[float] = []
    power: list[float] = []
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            times.append(float(row["host_time"]))
            power.append(float(row["power_mw"]))

    profile = json.loads(json_path.read_text())
    t0 = times[0]
    rel_t = [t - t0 for t in times]
    telem = profile["telemetry"]
    markers = {
        "armed": telem["armed"]["host_time"] - t0,
        "start": telem["start"]["host_time"] - t0,
        "done": telem["done"]["host_time"] - t0,
        "loops": profile["metadata"]["loops"],
        "elapsed_ms": float(telem["done"]["fields"]["elapsed_ms"]),
        "baseline_mw": profile.get("energy", {}).get("baseline_power_mw"),
    }
    return rel_t, power, markers


def plot_power_timeline(
    rel_t: list[float],
    power: list[float],
    markers: dict,
    out_stem: Path,
    title_suffix: str = "",
) -> None:
    armed_t = markers["armed"]
    start_t = markers["start"]
    done_t = markers["done"]
    elapsed_s = markers["elapsed_ms"] / 1000.0
    loops = markers["loops"]

    # Focus window: a little before ARMED through post-DONE tail.
    t_min = max(0.0, armed_t - 1.0)
    t_max = done_t + 2.5
    mask = [(t_min <= t <= t_max) for t in rel_t]
    xs = [t for t, m in zip(rel_t, mask) if m]
    ys = [p for p, m in zip(power, mask) if m]

    fig, ax = plt.subplots(figsize=(6.5, 3.2))
    ax.plot(xs, ys, color="#1f4e79", linewidth=1.0, label="INA219 power", zorder=3)

    # Phase shading
    ax.axvspan(t_min, armed_t, color="#e8f4e8", alpha=0.35, zorder=0)
    ax.axvspan(armed_t, start_t, color="#fff4cc", alpha=0.4, zorder=0)
    ax.axvspan(start_t, done_t, color="#dce9f8", alpha=0.45, zorder=0)
    ax.axvspan(done_t, t_max, color="#fde8e8", alpha=0.35, zorder=0)

    # Event lines
    for t_ev, label, color in (
        (armed_t, "ARMED", "#b8860b"),
        (start_t, "LOOP START", "#2e7d32"),
        (done_t, "LOOP DONE", "#c62828"),
    ):
        ax.axvline(t_ev, color=color, linestyle="--", linewidth=0.9, alpha=0.85, zorder=2)
        ax.text(
            t_ev,
            620,
            label,
            rotation=90,
            va="top",
            ha="right",
            fontsize=7,
            color=color,
            fontweight="bold",
        )

    baseline = markers.get("baseline_mw")
    if baseline:
        ax.axhline(baseline, color="#6a6a6a", linestyle=":", linewidth=0.9, alpha=0.7)
        ax.text(
            t_max - 0.15,
            baseline + 12,
            f"arming mean ≈ {baseline:.0f} mW",
            ha="right",
            fontsize=7,
            color="#555555",
        )

    ax.axhline(152, color="#888888", linestyle="-.", linewidth=0.7, alpha=0.6)
    ax.text(t_min + 0.05, 158, "idle ≈ 152 mW", fontsize=7, color="#666666")

    ax.set_xlabel("Time (seconds)")
    ax.set_ylabel("Power (mW)")
    title = f"ESP8266 warm-cache energy run ({loops:,} lookups"
    if title_suffix:
        title += f", {title_suffix}"
    title += f") — loop {elapsed_s:.2f} s"
    ax.set_title(title)
    ax.set_xlim(t_min, t_max)
    ax.set_ylim(100, 640)
    ax.grid(True, which="both")

    legend_handles = [
        mpatches.Patch(facecolor="#e8f4e8", edgecolor="none", label="Idle / pre-arm"),
        mpatches.Patch(facecolor="#fff4cc", edgecolor="none", label="3 s arming (Wi-Fi spikes)"),
        mpatches.Patch(facecolor="#dce9f8", edgecolor="none", label=f"Execution ({elapsed_s:.2f} s)"),
        mpatches.Patch(facecolor="#fde8e8", edgecolor="none", label="DONE telemetry spike"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", framealpha=0.92)

    fig.tight_layout()
    for ext in ("png", "pdf"):
        out_path = out_stem.with_suffix(f".{ext}")
        fig.savefig(out_path, bbox_inches="tight", dpi=300)
        print(f"[+] Saved {out_path}")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot INA219 power vs time for energy benchmark runs")
    parser.add_argument(
        "run_dirs",
        nargs="+",
        type=Path,
        help="Result directories containing ina219_samples.csv and energy_profile.json",
    )
    args = parser.parse_args()

    for run_dir in args.run_dirs:
        run_dir = run_dir.resolve()
        rel_t, power, markers = load_run(run_dir)
        stem = run_dir / "fig_power_over_time"
        loops = markers["loops"]
        plot_power_timeline(rel_t, power, markers, stem, title_suffix="")


if __name__ == "__main__":
    main()
