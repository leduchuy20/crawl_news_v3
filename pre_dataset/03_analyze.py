#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
03_analyze.py
-------------
Phân tích dataset đã clean + NER để verify chất lượng.
In ra thống kê về: entities, category, duplicates, content quality.

Hỗ trợ multi-partition input (glob).

Usage:
    # Default: đọc data/articles_ner*.jsonl
    python 03_analyze.py

    # Explicit
    python 03_analyze.py --input 'data/articles_ner*.jsonl'
    python 03_analyze.py --input 'data/articles_cleaned.jsonl*'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from partition_io import expand_inputs, iter_records


def analyze(input_patterns: List[str], top_k: int = 20):
    inputs = expand_inputs(input_patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {input_patterns}", file=sys.stderr)
        sys.exit(1)

    records = list(iter_records(inputs))
    total = len(records)
    print(f"=" * 60)
    print(f"DATASET ANALYSIS")
    print(f"=" * 60)
    print(f"Input files ({len(inputs)}):")
    for p in inputs:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p} ({size_mb:.1f} MB)")
    print(f"Total records: {total:,}")
    print()

    # ============ Source & category distribution ============
    print("By source:")
    for src, c in Counter(r.get("source", "") for r in records).most_common():
        print(f"  {src:<15} {c:>6,}  ({c/total*100:5.1f}%)")
    print()

    print("By category_normalized:")
    for cat, c in Counter(r.get("category_normalized", "") for r in records).most_common():
        print(f"  {cat:<15} {c:>6,}  ({c/total*100:5.1f}%)")
    print()

    # ============ Content quality ============
    print("Content quality:")
    full = sum(1 for r in records if r.get("has_full_content"))
    lens = [r.get("content_length", 0) for r in records]
    lens.sort()
    n = len(lens)
    avg = sum(lens) / n if n else 0
    p50 = lens[n // 2] if n else 0
    p95 = lens[int(n * 0.95)] if n else 0
    print(f"  has_full_content=True: {full:,} ({full/total*100:.1f}%)")
    print(f"  Content length avg   : {avg:,.0f}")
    print(f"  Content length p50   : {p50:,}")
    print(f"  Content length p95   : {p95:,}")
    print()

    # ============ Duplicates ============
    print("Duplicates:")
    has_dup_field = sum(1 for r in records if "dup_group_id" in r)
    dup_groups = defaultdict(int)
    for r in records:
        gid = r.get("dup_group_id", "")
        if gid:
            dup_groups[gid] += 1
    canonical = sum(1 for r in records if r.get("is_canonical") is True)
    print(f"  Records with dup_group_id field: {has_dup_field:,}")
    print(f"  Unique dup groups (>1 record)  : {len(dup_groups):,}")
    print(f"  Canonical records              : {canonical:,}")
    print()

    # ============ Author ============
    print("Author:")
    with_author = sum(1 for r in records if r.get("author"))
    print(f"  Records with author: {with_author:,} ({with_author/total*100:.1f}%)")
    if with_author:
        sample_authors = [r["author"] for r in records if r.get("author")][:10]
        print(f"  Sample: {sample_authors}")
    print()

    # ============ Keywords ============
    print("Keywords:")
    with_kw = sum(1 for r in records if r.get("keywords"))
    total_kw = sum(len(r.get("keywords", [])) for r in records)
    print(f"  Records with keywords: {with_kw:,}")
    print(f"  Total keyword mentions: {total_kw:,}")
    # Top keywords
    kw_counter = Counter()
    for r in records:
        for kw in r.get("keywords", []):
            kw_counter[kw] += 1
    print(f"  Top {top_k} keywords:")
    for kw, c in kw_counter.most_common(top_k):
        print(f"    {c:>4}  {kw}")
    print()

    # ============ Entities (NER output) ============
    print("Entities (NER):")
    with_ent = sum(1 for r in records if r.get("entities"))
    total_ent = sum(len(r.get("entities", [])) for r in records)
    print(f"  Records with entities: {with_ent:,} ({with_ent/total*100:.1f}%)")
    print(f"  Total entity mentions: {total_ent:,}")
    if total_ent == 0:
        print("  (No entities extracted — did you run 02_ner.py?)")
        print()
    else:
        # By type
        type_counter = Counter()
        for r in records:
            for e in r.get("entities", []):
                type_counter[e.get("type", "")] += 1
        print("  By type:")
        for t, c in type_counter.most_common():
            print(f"    {t:<8} {c:>6,}")
        print()

        # Top entities per type
        top_by_type: Dict[str, Counter] = defaultdict(Counter)
        for r in records:
            for e in r.get("entities", []):
                t = e.get("type", "")
                txt = e.get("text", "")
                if t and txt:
                    top_by_type[t][txt] += 1

        for t in ["PER", "LOC", "ORG", "MISC"]:
            if t not in top_by_type:
                continue
            print(f"  Top {top_k} {t}:")
            for txt, c in top_by_type[t].most_common(top_k):
                print(f"    {c:>4}  {txt}")
            print()

    # ============ Time range ============
    print("Time range:")
    dates = sorted(r.get("published_at", "") for r in records if r.get("published_at"))
    if dates:
        print(f"  Min: {dates[0]}")
        print(f"  Max: {dates[-1]}")
    pub_dates = Counter(r.get("publish_date", "") for r in records if r.get("publish_date"))
    if pub_dates:
        sorted_dates = sorted(pub_dates.items())
        print(f"  Most active dates:")
        for d, c in sorted(pub_dates.items(), key=lambda x: -x[1])[:5]:
            print(f"    {d}: {c:,}")
    print()

    # ============ Sample record ============
    print("Sample record (first):")
    if records:
        s = dict(records[0])
        # Truncate long fields
        if "content" in s and len(s["content"]) > 200:
            s["content"] = s["content"][:200] + "...[truncated]"
        print(json.dumps(s, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Analyze multi-partition JSONL dataset")
    ap.add_argument("--input", action="append", default=None,
                    help="Input pattern (glob). Có thể dùng nhiều lần. "
                         "Default: data/articles_ner*.jsonl")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    inputs = args.input or ["data/articles_ner*.jsonl"]
    analyze(inputs, args.top)


if __name__ == "__main__":
    main()
