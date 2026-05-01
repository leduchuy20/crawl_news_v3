#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
03_plot_results.py
------------------
Vẽ chart từ CSV benchmark results.

Output:
- results/chart_fulltext.png
- results/chart_aggregation.png
- results/chart_entity.png

Usage:
    python 03_plot_results.py --csv results/benchmark_XXXXX.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from typing import Dict, List

try:
    import matplotlib.pyplot as plt
    import matplotlib
    matplotlib.use("Agg")
except ImportError:
    print("ERROR: pip install matplotlib", file=sys.stderr)
    sys.exit(1)


# Engine colors — semantic
ENGINE_COLORS = {
    "Elasticsearch":      "#0071e3",
    "ClickHouse-MV":      "#14b8a6",
    "ClickHouse-raw":     "#2dd4bf",
    "Postgres":           "#a855f7",
    "PG-tsvector":        "#8b5cf6",
    "PG-trigram":         "#a78bfa",
    "PG-seqscan":         "#ef4444",  # red = baseline tệ
}


def load_csv(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def plot_category(rows: List[Dict], category: str, out_path: str, log_scale: bool = True):
    """
    Horizontal grouped bar chart: scenario (y-axis) × engine (color groups).
    """
    cat_rows = [r for r in rows if r["category"] == category and r.get("median_ms")]
    if not cat_rows:
        print(f"  (no data for {category})")
        return

    # Group: scenario → {engine: median}
    scenarios = []
    data = defaultdict(dict)  # scenario → {engine: median_ms}
    for r in cat_rows:
        scen = r["scenario"]
        eng = r["engine"]
        if scen not in scenarios:
            scenarios.append(scen)
        data[scen][eng] = float(r["median_ms"])

    engines = []
    for s in scenarios:
        for e in data[s]:
            if e not in engines:
                engines.append(e)

    # Plot
    n_scen = len(scenarios)
    n_eng = len(engines)
    bar_height = 0.8 / n_eng
    fig, ax = plt.subplots(figsize=(11, max(4, n_scen * 1.1)))

    y_positions = list(range(n_scen))
    for i, engine in enumerate(engines):
        values = [data[s].get(engine, 0) for s in scenarios]
        offsets = [y + (i - n_eng / 2) * bar_height + bar_height / 2 for y in y_positions]
        color = ENGINE_COLORS.get(engine, "#888")
        bars = ax.barh(offsets, values, height=bar_height, label=engine, color=color)
        # Label on bar
        for b, v in zip(bars, values):
            if v > 0:
                ax.text(v * 1.05, b.get_y() + b.get_height() / 2,
                        f"{v:.1f}ms", va="center", fontsize=9, color="#333")

    ax.set_yticks(y_positions)
    ax.set_yticklabels(scenarios)
    ax.invert_yaxis()
    ax.set_xlabel("Median latency (ms) — lower is better")
    ax.set_title(f"Benchmark: {category.upper()}")
    if log_scale:
        ax.set_xscale("log")
        ax.set_xlabel("Median latency (ms, log scale) — lower is better")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.legend(loc="lower right", framealpha=0.95)
    ax.set_axisbelow(True)

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"  ✓ {out_path}")


def plot_speedup_vs_postgres(rows: List[Dict], out_path: str):
    """
    Horizontal bar: scenario × speedup multiplier (ES or CH vs Postgres).
    """
    # Collect PG baseline for each scenario
    pg_baseline = {}  # scenario → median_ms của Postgres/PG-tsvector
    for r in rows:
        if not r.get("median_ms"):
            continue
        scen = r["scenario"]
        eng = r["engine"]
        # PG "best-effort" baseline: pick tsvector cho fulltext, Postgres cho aggregation
        if eng in ("PG-tsvector", "Postgres"):
            if scen not in pg_baseline or float(r["median_ms"]) < pg_baseline[scen]:
                pg_baseline[scen] = float(r["median_ms"])

    # Compute speedup for ES/ClickHouse
    speedups = []  # (scenario, engine, speedup)
    for r in rows:
        if not r.get("median_ms"):
            continue
        scen = r["scenario"]
        eng = r["engine"]
        if eng not in ("Elasticsearch", "ClickHouse-MV"):
            continue
        if scen not in pg_baseline:
            continue
        speedup = pg_baseline[scen] / float(r["median_ms"])
        speedups.append((scen, eng, speedup))

    if not speedups:
        print("  (no speedup data)")
        return

    # Sort by speedup desc
    speedups.sort(key=lambda x: -x[2])
    labels = [f"{s[0]}\n[{s[1]}]" for s in speedups]
    values = [s[2] for s in speedups]
    colors = [ENGINE_COLORS.get(s[1], "#888") for s in speedups]

    fig, ax = plt.subplots(figsize=(11, max(4, len(speedups) * 0.4)))
    bars = ax.barh(range(len(speedups)), values, color=colors)
    ax.set_yticks(range(len(speedups)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.axvline(x=1, color="#888", linestyle="--", label="PG baseline (1×)")
    ax.set_xlabel("Speedup vs Postgres (higher = faster)")
    ax.set_title("Speedup of ES / ClickHouse vs Postgres")
    ax.set_xscale("log")
    for b, v in zip(bars, values):
        ax.text(v * 1.08, b.get_y() + b.get_height() / 2,
                f"{v:.1f}×", va="center", fontsize=9, color="#333")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.set_axisbelow(True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"  ✓ {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--outdir", default=None, help="Default: same folder as CSV")
    ap.add_argument("--no-log", action="store_true", help="Disable log scale")
    args = ap.parse_args()

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.csv))
    os.makedirs(outdir, exist_ok=True)

    rows = load_csv(args.csv)
    print(f"Loaded {len(rows)} rows from {args.csv}")
    print()
    print("Rendering charts...")

    for cat in ["fulltext", "aggregation", "entity"]:
        plot_category(
            rows, cat,
            os.path.join(outdir, f"chart_{cat}.png"),
            log_scale=not args.no_log,
        )

    plot_speedup_vs_postgres(rows, os.path.join(outdir, "chart_speedup.png"))

    print()
    print("Done. Dùng các PNG này chèn vào báo cáo/slide.")


if __name__ == "__main__":
    main()
