#!/usr/bin/env python3
"""Generate adversarial evaluation figure for paper/main.tex from curated results."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
SIM_PATH = REPO / "benchmark/results/adversarial_sim_20260525_164210/adversarial_simulation.json"
OUT_DIR = REPO / "paper/figures"
OUT_DIR.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 9,
    "figure.dpi": 300,
})


def load_hw_summary(folder: str) -> dict:
    p = REPO / "benchmark/results" / folder / "adversarial_hardware.json"
    return json.loads(p.read_text())["scenarios"][0]["summary"]


def main() -> None:
    hw_rows = [
        ("$f{=}0$", load_hw_summary("adversarial_f0_20260526_003110")),
        ("$f{=}1$", load_hw_summary("adversarial_f1_20260526_003649")),
        ("$f{=}2$", load_hw_summary("adversarial_f2_20260526_004338")),
        ("L4 equivoc.", load_hw_summary("hw_l4_20260525_174557")),
        ("Sybil ($k{=}3$)", load_hw_summary("hw_sybil3_20260525_181451")),
    ]
    labels = [r[0] for r in hw_rows]
    success = [100 * r[1]["success_rate"] for r in hw_rows]
    false_accept = [100 * r[1]["false_accept_rate"] for r in hw_rows]

    sim = json.loads(SIM_PATH.read_text())
    sybil_rows = []
    for suite in sim["suites"]:
        if suite["test"] == "sybil":
            for row in suite["rows"]:
                if row.get("n_physical") == 5:
                    sybil_rows.append(row)
            break
    sybil_rows.sort(key=lambda r: r["sybil_extra_keys"])
    k_vals = [r["sybil_extra_keys"] for r in sybil_rows]
    fa_sybil = [100 * r["false_accept_rate"] for r in sybil_rows]

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6))

    ax = axes[0]
    x = np.arange(len(labels))
    w = 0.35
    ax.bar(x - w / 2, success, w, label="Success", color="#2ca02c", edgecolor="black", linewidth=0.4)
    ax.bar(x + w / 2, false_accept, w, label="False accept", color="#d62728", edgecolor="black", linewidth=0.4)
    ax.set_ylabel("Rate (\\%)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylim(0, 105)
    ax.legend(loc="upper right", fontsize=7)
    ax.grid(axis="y", alpha=0.3, linestyle=":")
    ax.set_title("(a) Hardware spot-check (5 rounds each)", fontsize=9, fontweight="bold")

    ax2 = axes[1]
    ax2.plot(k_vals, fa_sybil, "o-", color="#d62728", linewidth=1.5, markersize=5)
    ax2.axvline(3, color="#888888", linestyle="--", linewidth=0.8, label="HW Sybil $k{=}3$")
    ax2.scatter([3], [40], s=60, facecolors="none", edgecolors="#1f77b4", linewidths=2, zorder=5,
                label="HW measured (40\\%)")
    ax2.set_xlabel("Extra Ed25519 identities ($k$)")
    ax2.set_ylabel("False accept rate (\\%)")
    ax2.set_ylim(-5, 105)
    ax2.set_xticks(k_vals)
    ax2.grid(alpha=0.3, linestyle=":")
    ax2.legend(loc="upper left", fontsize=7)
    ax2.set_title("(b) Simulated Sybil ($N_{\\mathrm{phys}}{=}5$)", fontsize=9, fontweight="bold")

    plt.tight_layout()
    out = OUT_DIR / "fig_adversarial_evaluation.pdf"
    plt.savefig(out, bbox_inches="tight")
    plt.savefig(out.with_suffix(".png"), bbox_inches="tight")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
