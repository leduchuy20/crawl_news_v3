#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
05_plot_storage.py
------------------
Vẽ chart từ output của 04_storage_compare.py:
  - chart_storage_total.png   — horizontal stacked bar: data + index per engine
                                + vertical line đánh dấu raw JSONL baseline.
  - chart_storage_breakdown.png — grouped bar: disk_size per table per engine.

Usage:
    python 05_plot_storage.py --json results/storage_XXXX.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("ERROR: pip install matplotlib", file=sys.stderr)
    sys.exit(1)


ENGINE_COLORS = {
    "Raw JSONL":     "#94a3b8",
    "PostgreSQL":    "#a855f7",
    "MongoDB":       "#10b981",
    "Elasticsearch": "#0071e3",
    "ClickHouse":    "#14b8a6",
}

# Data vs index gradient (data đậm, index nhạt)
DATA_COLOR  = lambda c: c
INDEX_COLOR = lambda c: c + "80"  # 50% alpha hex


def fmt_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def plot_total(snapshots: List[Dict], raw_bytes: int, out_path: str):
    """Horizontal stacked bar: data (đậm) + index (nhạt) per engine."""
    engines = [s["engine"] for s in snapshots if s["engine"] != "Raw JSONL"]
    data_sums = []
    idx_sums = []
    for s in snapshots:
        if s["engine"] == "Raw JSONL":
            continue
        data_sums.append(sum(e["data_size"] for e in s["entries"]))
        idx_sums.append(sum(e.get("index_size", 0) for e in s["entries"]))

    if not engines:
        print("  (no engine data)")
        return

    fig, ax = plt.subplots(figsize=(11, max(3.5, len(engines) * 0.9)))
    y = list(range(len(engines)))

    # Stacked: data first, then index
    data_colors = [DATA_COLOR(ENGINE_COLORS.get(e, "#888")) for e in engines]
    idx_colors  = [INDEX_COLOR(ENGINE_COLORS.get(e, "#888")) for e in engines]

    bars_data = ax.barh(y, data_sums, color=data_colors, label="Data")
    bars_idx  = ax.barh(y, idx_sums, left=data_sums, color=idx_colors,
                        label="Indexes", hatch="//", edgecolor="white", linewidth=0.5)

    # Annotate totals on the right
    for i, e in enumerate(engines):
        total = data_sums[i] + idx_sums[i]
        ratio = total / raw_bytes if raw_bytes > 0 else 0
        ax.text(total * 1.01, i,
                f"{fmt_mb(total)}  ({ratio:.2f}× raw)",
                va="center", fontsize=9, color="#333")

    # Raw JSONL baseline as vertical line
    ax.axvline(raw_bytes, color="#94a3b8", linestyle="--", linewidth=1.5,
               label=f"Raw JSONL = {fmt_mb(raw_bytes)}")

    ax.set_yticks(y)
    ax.set_yticklabels(engines)
    ax.invert_yaxis()
    ax.set_xlabel("Disk usage (bytes)")
    ax.set_title("Storage on disk — Data + Indexes per engine\n(thấp hơn = tiết kiệm hơn)")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.legend(loc="lower right", framealpha=0.95)
    ax.set_axisbelow(True)

    # Tick labels in MB
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda x, _: f"{x/1024/1024:.0f}M"))
    # Extend xlim for the text labels
    xmax = max(data_sums[i] + idx_sums[i] for i in range(len(engines)))
    ax.set_xlim(0, xmax * 1.30)

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"  ✓ {out_path}")


def plot_breakdown(snapshots: List[Dict], out_path: str):
    """Grouped bar: disk_size per (table, engine).
    Y-axis = table name, x-axis = disk_size, colored by engine.
    """
    # Collect: table → engine → disk_size
    tables = []
    data: Dict[str, Dict[str, int]] = {}
    engines_seen: List[str] = []
    for s in snapshots:
        eng = s["engine"]
        if eng == "Raw JSONL":
            continue
        if eng not in engines_seen:
            engines_seen.append(eng)
        for e in s["entries"]:
            t = e["table"]
            if t not in data:
                data[t] = {}
                tables.append(t)
            data[t][eng] = e["disk_size"]

    if not tables:
        print("  (no breakdown data)")
        return

    n_tables = len(tables)
    n_eng = len(engines_seen)
    bar_h = 0.8 / n_eng

    fig, ax = plt.subplots(figsize=(11, max(4, n_tables * 1.2)))
    y_pos = list(range(n_tables))

    for i, eng in enumerate(engines_seen):
        vals = [data[t].get(eng, 0) for t in tables]
        offsets = [y + (i - n_eng / 2) * bar_h + bar_h / 2 for y in y_pos]
        color = ENGINE_COLORS.get(eng, "#888")
        bars = ax.barh(offsets, vals, height=bar_h, label=eng, color=color)
        for b, v in zip(bars, vals):
            if v > 0:
                ax.text(v * 1.02, b.get_y() + b.get_height() / 2,
                        fmt_mb(v), va="center", fontsize=8, color="#333")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(tables, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("Disk size (bytes)")
    ax.set_title("Disk size per table/collection across engines")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.legend(loc="lower right", framealpha=0.95)
    ax.set_axisbelow(True)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda x, _: f"{x/1024/1024:.0f}M"))
    # Extend xlim for labels
    all_vals = [v for t in tables for v in data[t].values() if v > 0]
    if all_vals:
        ax.set_xlim(0, max(all_vals) * 1.25)

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"  ✓ {out_path}")


def find_latest(pattern: str) -> str:
    import glob as _glob
    files = _glob.glob(pattern)
    if not files:
        return ""
    return max(files, key=os.path.getmtime)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None,
                    help="Path to storage_<ts>.json. Default: file mới nhất "
                         "trong results/storage_*.json")
    ap.add_argument("--outdir", default=None, help="Default: same folder as JSON")
    args = ap.parse_args()

    json_path = args.json or find_latest("results/storage_*.json")
    if not json_path:
        print("ERROR: no --json given và không tìm thấy results/storage_*.json",
              file=sys.stderr)
        sys.exit(1)
    print(f"Reading: {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    snapshots = payload["snapshots"]
    raw_bytes = payload["raw_jsonl_bytes"]

    outdir = args.outdir or os.path.dirname(os.path.abspath(json_path))
    os.makedirs(outdir, exist_ok=True)

    print("Rendering storage charts...")
    plot_total(snapshots, raw_bytes,
               os.path.join(outdir, "chart_storage_total.png"))
    plot_breakdown(snapshots,
                   os.path.join(outdir, "chart_storage_breakdown.png"))

    print()
    print("Done. Dùng các PNG này chèn vào slide / báo cáo.")


if __name__ == "__main__":
    main()
