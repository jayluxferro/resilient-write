#!/usr/bin/env python3
"""Generate publication-quality PDF figures for the resilient-write paper."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Global style
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "font.family": "serif",
    "font.size": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "pdf.fonttype": 42,       # TrueType fonts in PDF
    "ps.fonttype": 42,
})


def fig_failure_comparison():
    """Bar chart comparing Naive, Defensive, and Resilient-Write."""
    metrics = [
        "Recovery time\n(seconds)",
        "Data loss\nprob. (%)",
        "Self-correction\nrate (%)",
        "Wasted tool\ncalls (%)",
    ]
    naive       = [10.0,  5.0,  5.0, 25.0]
    defensive   = [ 5.5,  1.0, 15.0, 12.5]
    resilient   = [ 2.0,  0.1, 65.0,  3.0]

    x = np.arange(len(metrics))
    width = 0.25

    colors = ["#2196F3", "#FF9800", "#4CAF50"]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(x - width, naive,     width, label="Naive",            color=colors[0])
    ax.bar(x,         defensive, width, label="Defensive",        color=colors[1])
    ax.bar(x + width, resilient, width, label="Resilient-Write",  color=colors[2])

    ax.set_xticks(x)
    ax.set_xticklabels(metrics)
    ax.set_ylabel("Metric value")
    ax.legend(loc="upper right", frameon=False)

    fig.tight_layout()
    out = OUTPUT_DIR / "fig_failure_comparison.pdf"
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def fig_layer_coverage():
    """Heatmap of failure modes vs layers."""
    failure_modes = [
        "Content filter",
        "Truncation",
        "Partial write",
        "Retry thrashing",
        "Opaque errors",
        "Session loss",
        "Secret leakage",
        "Handoff failure",
    ]
    layers = [
        "L0\nRisk Score",
        "L1\nSafe Write",
        "L2\nChunks",
        "L3\nTyped Errors",
        "L4\nScratchpad",
        "L5\nHandoff",
    ]

    data = np.array([
        [1.0, 0.5, 0.0, 0.5, 0.0, 0.0],  # Content filter
        [0.0, 1.0, 0.5, 0.5, 0.0, 0.0],  # Truncation
        [0.0, 1.0, 0.5, 0.0, 0.0, 0.0],  # Partial write
        [0.5, 0.0, 0.0, 1.0, 0.0, 0.0],  # Retry thrashing
        [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],  # Opaque errors
        [0.0, 0.0, 0.5, 0.0, 0.0, 1.0],  # Session loss
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],  # Secret leakage
        [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],  # Handoff failure
    ])

    fig, ax = plt.subplots(figsize=(7, 5))
    im = ax.imshow(data, cmap="Greens", aspect="auto", vmin=0, vmax=1)

    ax.set_xticks(np.arange(len(layers)))
    ax.set_yticks(np.arange(len(failure_modes)))
    ax.set_xticklabels(layers, ha="center")
    ax.set_yticklabels(failure_modes)

    # Annotate each cell
    for i in range(len(failure_modes)):
        for j in range(len(layers)):
            val = data[i, j]
            color = "white" if val > 0.7 else "black"
            ax.text(j, i, f"{val:.1f}", ha="center", va="center",
                    color=color, fontsize=10)

    fig.tight_layout()
    out = OUTPUT_DIR / "fig_layer_coverage.pdf"
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def fig_test_coverage():
    """Pie chart of test distribution by layer."""
    labels = [
        "L0 Risk Score",
        "L1 Safe Write",
        "L2 Chunks",
        "L3 Errors",
        "L4 Scratchpad",
        "L5 Handoff",
        "New Features",
        "Infrastructure",
    ]
    counts = [28, 17, 27, 27, 21, 8, 42, 16]

    colors = [
        "#2196F3",  # blue
        "#4CAF50",  # green
        "#FF9800",  # orange
        "#F44336",  # red
        "#9C27B0",  # purple
        "#00BCD4",  # cyan
        "#FFC107",  # amber
        "#607D8B",  # blue-grey
    ]

    def fmt(pct, allvals):
        absolute = int(round(pct / 100.0 * sum(allvals)))
        return f"{pct:.1f}%\n({absolute})"

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, texts, autotexts = ax.pie(
        counts,
        labels=labels,
        colors=colors,
        autopct=lambda pct: fmt(pct, counts),
        startangle=140,
        pctdistance=0.75,
    )
    for t in autotexts:
        t.set_fontsize(9)

    fig.tight_layout()
    out = OUTPUT_DIR / "fig_test_coverage.pdf"
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


if __name__ == "__main__":
    print("Generating figures...")
    fig_failure_comparison()
    fig_layer_coverage()
    fig_test_coverage()
    print("Done.")
