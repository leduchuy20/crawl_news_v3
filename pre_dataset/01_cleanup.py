#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01_cleanup.py
-------------
Bước 1 — Post-processing cleanup cho dataset đã crawl.

Chạy FULL REBUILD mỗi lần (cần dedup toàn bộ dataset). Fast: ~1-2 phút/150k bài.

Xử lý:
1. Fix `author`: xóa các giá trị fallback (author == source)
2. Mở rộng CATEGORY_MAP để giảm tỉ lệ 'other' xuống mức tối thiểu
3. Dedup cross-source: gắn `dup_group_id`, đánh dấu `is_canonical`
4. Thêm derived fields cho ClickHouse time-series:
   - `publish_date`     — YYYY-MM-DD theo giờ VN
   - `publish_hour`     — 0-23
   - `publish_dow`      — 0=Mon..6=Sun
   - `word_count`       — số từ trong content

Hỗ trợ multi-partition input (glob) + output tự rotate ở 89MB.

Usage:
    # Default: đọc articles_final*.jsonl + migrated_*.jsonl
    python 01_cleanup.py

    # Explicit
    python 01_cleanup.py \\
        --input 'data/articles_final*.jsonl' \\
        --input 'data/migrated_*.jsonl' \\
        --output data/articles_cleaned.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from partition_io import (
    expand_inputs,
    iter_records,
    clear_all_partitions,
    all_partition_paths,
    PartitionedJsonlWriter,
)

VN_TZ = timezone(timedelta(hours=7))


# ====================================================================
# Category map — MỞ RỘNG so với crawler
# ====================================================================
# Dựa trên phân tích thực tế dataset hiện có
CATEGORY_MAP_EXTENDED = {
    # sports
    "the-thao": "sports", "thể thao": "sports", "bong-da": "sports",
    "bóng đá việt nam": "sports", "các môn khác": "sports",
    "xe": "sports", "sea-games-32": "sports", "asiancup2019": "sports",

    # news (tin tức, xã hội, tin nóng)
    "thoi-su": "news", "thời sự": "news", "tin-tuc-trong-ngay": "news",
    "xa-hoi": "news", "xã hội": "news", "tin-moi-nhat": "news",
    "tin tức": "news", "tin nóng": "news", "dân sinh": "news",
    "bạn đọc": "news", "59s": "news", "video": "news",
    "đô thị": "news", "giao thông": "news",

    # world
    "the-gioi": "world", "thế giới": "world", "quoc-te": "world",
    "quốc tế": "world", "asean": "world", "chau-a-tbd": "world",
    "chau-au": "world", "chau-my": "world", "chau-phi": "world",
    "trung-dong": "world",

    # politics
    "chinh-tri": "politics", "chính trị": "politics",
    "xay-dung-dang": "politics", "xa-luan": "politics",
    "chính quyền": "politics", "đảng": "politics",

    # business / economy / finance
    "kinh-doanh": "business", "kinh-te": "business", "kinh doanh": "business",
    "kinh tế": "business", "kinh-doanh-tai-chinh": "business",
    "taichinh": "business", "tài chính": "business",
    "chungkhoan": "business", "chứng khoán": "business",
    "tai-chinh-bat-dong-san": "business", "bat-dong-san": "business",
    "bất động sản": "business", "thi-truong": "business",
    "thị trường": "business", "tiền tệ & đầu tư": "business",
    "đầu tư": "business", "tư vấn tài chính": "business",
    "net zero": "business", "lao động": "business",

    # entertainment
    "giai-tri": "entertainment", "giải trí": "entertainment",
    "van-hoa": "entertainment", "văn hóa": "entertainment",
    "văn hóa - văn nghệ": "entertainment",
    "van-hoa-giai-tri": "entertainment", "van-hoa-van-nghe": "entertainment",
    "phim": "entertainment", "xuat-ban": "entertainment",
    "xuất bản": "entertainment", "nghệ sĩ": "entertainment",
    "chân dung": "entertainment",

    # lifestyle
    "doi-song": "lifestyle", "đời sống": "lifestyle",
    "gia-dinh": "lifestyle", "gia đình": "lifestyle",
    "ban-tre-cuoc-song": "lifestyle", "thoi-trang": "lifestyle",
    "thời trang": "lifestyle", "giới trẻ": "lifestyle",
    "góc phụ huynh": "lifestyle",

    # law
    "phap-luat": "law", "pháp luật": "law",

    # education
    "giao-duc": "education", "giáo dục": "education",
    "giao-duc-du-hoc": "education", "giao-duc-khoa-hoc": "education",
    "giaoduc": "education", "nhà trường": "education",
    "tuyển sinh": "education", "du học": "education",

    # health
    "suc-khoe": "health", "sức khỏe": "health", "y-te": "health",
    "yte": "health", "dinh dưỡng": "health",

    # tech
    "cong-nghe": "tech", "công nghệ": "tech", "hi-tech": "tech",
    "so-hoa": "tech", "khoa-hoc": "tech", "khoa học": "tech",
    "ai-365": "tech", "ai contest": "tech",

    # travel
    "du-lich": "travel", "du lịch": "travel", "du-lich-xanh": "travel",

    # auto
    "oto-xe-may": "auto", "o-to-xe-may": "auto",

    # military
    "quan-su": "military", "quân sự": "military",

    # environment
    "môi trường": "environment", "moi-truong": "environment",

    # other → giữ nguyên
    "trắc nghiệm": "other", "trac-nghiem": "other",
    "lao động cuối tuần": "other",
    "trang-chu": "home", "home": "home", "tin-moi": "home",
}


def normalize_category_extended(raw: str) -> str:
    """Normalize với map mở rộng. Fallback substring matching."""
    if not raw:
        return "other"
    key = raw.strip().lower()
    if key in CATEGORY_MAP_EXTENDED:
        return CATEGORY_MAP_EXTENDED[key]
    # Fuzzy fallback
    for k, v in CATEGORY_MAP_EXTENDED.items():
        if k in key or key in k:
            return v
    return "other"


# ====================================================================
# Source names (để detect author == source)
# ====================================================================
KNOWN_SOURCE_NAMES = {
    "znews", "vnexpress", "dantri", "tuoitre", "thanhnien", "nld",
    "soha", "nhandan", "vietnamnet", "vietnamplus", "baotintuc",
    "kienthuc", "laodong", "24h",
    "zingnews", "rss_feed",
}


def fix_author(author: str, source: str) -> str:
    """Nếu author trùng source (bug fallback) → trả rỗng."""
    if not author:
        return ""
    a = author.strip().lower()
    if a == source.lower():
        return ""
    if a in KNOWN_SOURCE_NAMES:
        return ""
    return author.strip()


# ====================================================================
# Title normalization cho dedup
# ====================================================================
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def remove_vietnamese_tones(s: str) -> str:
    """Bỏ dấu tiếng Việt."""
    nfkd = unicodedata.normalize("NFKD", s)
    out = "".join(c for c in nfkd if not unicodedata.combining(c))
    out = out.replace("đ", "d").replace("Đ", "D")
    return out


def normalize_title(title: str) -> str:
    """Chuẩn hóa title cho dedup: lowercase, bỏ dấu, bỏ punctuation, chuẩn whitespace."""
    if not title:
        return ""
    t = title.strip().lower()
    t = remove_vietnamese_tones(t)
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def title_hash(title: str) -> str:
    """Hash để gắn dup_group_id."""
    norm = normalize_title(title)
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


# ====================================================================
# Time-series derived fields
# ====================================================================
def derive_time_fields(published_at: str) -> Dict[str, Any]:
    """Từ ISO UTC → VN local date/hour/dow."""
    if not published_at:
        return {"publish_date": "", "publish_hour": -1, "publish_dow": -1}
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(VN_TZ)
        return {
            "publish_date": local.date().isoformat(),
            "publish_hour": local.hour,
            "publish_dow": local.weekday(),  # 0=Mon..6=Sun
        }
    except Exception:
        return {"publish_date": "", "publish_hour": -1, "publish_dow": -1}


def count_words(content: str) -> int:
    """Đếm số từ (split theo whitespace)."""
    if not content:
        return 0
    return len(content.split())


# ====================================================================
# MAIN — FULL REBUILD với multi-partition IO
# ====================================================================
def cleanup(input_patterns: List[str], output_path: str, min_content_len: int = 200):
    """Đọc nhiều file input (hỗ trợ glob), cleanup với dedup toàn bộ, ghi output rotation."""
    # 0. Xoá output partitions cũ (full rebuild, không incremental)
    removed = clear_all_partitions(output_path)
    if removed:
        print(f"[0/3] Removed {removed} existing output partition(s)")

    # 1. Expand input + load all records (dedup cần giữ hết trong RAM cho title_hash)
    inputs = expand_inputs(input_patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {input_patterns}", file=sys.stderr)
        sys.exit(1)
    print(f"[1/3] Loading {len(inputs)} input file(s):")
    for p in inputs:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"        {p} ({size_mb:.1f} MB)")

    records = []
    seen_ids = set()
    dup_across_inputs = 0
    for rec in iter_records(inputs):
        rid = rec.get("id")
        if not rid:
            continue
        if rid in seen_ids:
            dup_across_inputs += 1
            continue
        seen_ids.add(rid)
        records.append(rec)
    print(f"      Loaded {len(records):,} unique records ({dup_across_inputs:,} dup across inputs dropped)")

    # 2. Title-hash group dedup (toàn bộ dataset)
    print("[2/3] Grouping duplicates by normalized title...")
    for r in records:
        r["_title_hash"] = title_hash(r.get("title", ""))

    groups = defaultdict(list)
    for r in records:
        groups[r["_title_hash"]].append(r)

    for group_id, bucket in groups.items():
        if len(bucket) == 1:
            bucket[0]["dup_group_id"] = ""
            bucket[0]["is_canonical"] = True
            bucket[0]["dup_count"] = 1
        else:
            bucket.sort(
                key=lambda r: (r.get("has_full_content", False), r.get("content_length", 0)),
                reverse=True,
            )
            for i, r in enumerate(bucket):
                r["dup_group_id"] = group_id
                r["is_canonical"] = (i == 0)
                r["dup_count"] = len(bucket)

    dup_groups = sum(1 for b in groups.values() if len(b) > 1)
    dup_records = sum(len(b) for b in groups.values() if len(b) > 1)
    print(f"      Found {dup_groups:,} duplicate groups ({dup_records:,} records)")

    # 3. Apply fixes + write với PartitionedJsonlWriter
    print(f"[3/3] Writing cleaned output to {output_path} (rotation at 89MB)...")
    stats = Counter()
    with PartitionedJsonlWriter(output_path) as writer:
        # Sort records theo published_at để output có thứ tự thời gian
        records.sort(key=lambda r: r.get("published_at", ""))
        for r in records:
            if r.get("content_length", 0) < min_content_len:
                stats["skipped_short"] += 1
                continue

            original_author = r.get("author", "")
            fixed_author = fix_author(original_author, r.get("source", ""))
            if original_author and not fixed_author:
                stats["author_cleared"] += 1
            r["author"] = fixed_author

            old_norm = r.get("category_normalized", "other")
            new_norm = normalize_category_extended(r.get("category_raw", ""))
            r["category_normalized"] = new_norm
            if old_norm == "other" and new_norm != "other":
                stats["category_resolved"] += 1

            t_fields = derive_time_fields(r.get("published_at", ""))
            r.update(t_fields)

            r["word_count"] = count_words(r.get("content", ""))

            r.pop("_title_hash", None)

            writer.write(r)
            stats["written"] += 1

    # Report
    print()
    print("=" * 60)
    print("CLEANUP STATS")
    print("=" * 60)
    print(f"  Written          : {stats['written']:,}")
    print(f"  Skipped (short)  : {stats['skipped_short']:,}")
    print(f"  Author cleared   : {stats['author_cleared']:,}  (author == source)")
    print(f"  Category resolved: {stats['category_resolved']:,}  (other → mapped)")
    print(f"  Duplicate groups : {dup_groups:,}")
    print(f"  Duplicate records: {dup_records:,}")
    print("=" * 60)

    # Verify: partition sizes + distribution
    print()
    print("Output partitions:")
    for p in all_partition_paths(output_path):
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p}: {size_mb:.1f} MB")

    print()
    print("Category distribution AFTER cleanup:")
    cat_counter = Counter()
    for p in all_partition_paths(output_path):
        for rec in iter_records([p]):
            cat_counter[rec.get("category_normalized", "")] += 1
    total = sum(cat_counter.values())
    for cat, c in cat_counter.most_common():
        pct = c / total * 100 if total else 0
        print(f"  {cat:<15} {c:>6,}  ({pct:5.1f}%)")

    return stats


def main():
    ap = argparse.ArgumentParser(description="Cleanup multi-partition JSONL dataset")
    ap.add_argument("--input", action="append", default=None,
                    help="Input pattern (glob). Có thể dùng nhiều lần. "
                         "Default: data/articles_final*.jsonl + data/migrated_*.jsonl")
    ap.add_argument("--output", default="data/articles_cleaned.jsonl",
                    help="Base output path (default: data/articles_cleaned.jsonl)")
    ap.add_argument("--min-content-len", type=int, default=200,
                    help="Bỏ bài có content ngắn hơn N chars (default: 200)")
    args = ap.parse_args()

    inputs = args.input or ["data/articles_final*.jsonl", "data/migrated_*.jsonl"]
    cleanup(inputs, args.output, args.min_content_len)


if __name__ == "__main__":
    main()
