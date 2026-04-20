#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01_clean_entities.py
--------------------
Clean noise entities sau NER trước khi index vào ES/ClickHouse.

Hỗ trợ multi-partition input (glob) + output tự rotate ở 89MB, dùng chung helper
với pre_dataset/partition_io.py.

Rules clean (thứ tự quan trọng):
1. Strip leading/trailing punctuation & whitespace
2. Reject pure-numeric / percent / currency
3. Reject date-like: "năm 2026", "tháng 3", "ngày 15"
4. Reject too long (>50 chars): thường do tokenizer gom nhầm nhiều entity
5. Reject common generic words (Bộ, Điểm, Người...)
6. Strip admin prefix: "xã Sơn Cẩm" → "Sơn Cẩm" để dedup với "Sơn Cẩm" khác
7. Dedup trong cùng bài (sau normalize)
8. (Optional) Re-classify: "xã/huyện/tỉnh X" luôn là LOC kể cả nếu NER gán PER

Usage:
    # Default: đọc ../data/articles_ner*.jsonl, ghi ../data/articles_ready.jsonl
    python 01_clean_entities.py

    # Explicit
    python 01_clean_entities.py \\
        --input '../data/articles_ner*.jsonl' \\
        --output ../data/articles_ready.jsonl
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pre_dataset"))
from partition_io import (
    expand_inputs,
    iter_records,
    clear_all_partitions,
    all_partition_paths,
    PartitionedJsonlWriter,
)


# ====================================================================
# Config — có thể tune
# ====================================================================
MIN_ENTITY_LEN = 2
MAX_ENTITY_LEN = 50

# Common words bị NER gán nhầm là entity (quá generic, không mang thông tin)
COMMON_WORDS_REJECT = {
    "bộ", "điểm", "người", "năm", "tuần", "tháng", "ngày", "giờ", "phút",
    "bên", "phía", "chiều", "sáng", "tối", "trưa", "đêm",
    "ông", "bà", "anh", "chị", "em", "cô", "chú", "bác",
    "ta", "mình", "họ", "chúng", "nhau",
    "đây", "đó", "kia", "này", "ấy",
    "trên", "dưới", "trong", "ngoài", "giữa",
    "nhà", "cửa", "đường", "phố",
}

# Prefix hành chính: nếu bắt đầu bằng các từ này → strip & force type=LOC
ADMIN_LOC_PREFIXES = [
    "xã ", "phường ", "thị trấn ", "thị xã ",
    "huyện ", "quận ", "thành phố ", "tỉnh ",
    "tp ", "tp. ", "tp.", "t.p ",
    "khu phố ", "ấp ", "thôn ", "bản ", "làng ",
    "khu ", "khu vực ",
    "phía bắc ", "phía nam ", "phía đông ", "phía tây ",
    "miền bắc ", "miền nam ", "miền trung ", "miền tây ",
    "vùng ",
]

# Prefix chức danh → force type=PER (nếu đã là PER thì strip để dedup)
PER_TITLE_PREFIXES = [
    "ông ", "bà ", "anh ", "chị ", "em ",
    "tổng thống ", "thủ tướng ", "chủ tịch ", "bộ trưởng ",
    "giáo sư ", "tiến sĩ ", "thạc sĩ ", "bác sĩ ", "kỹ sư ",
    "ts. ", "ths. ", "bs. ", "ks. ", "pgs. ",
    "hlv ", "huấn luyện viên ",
    "ca sĩ ", "nghệ sĩ ", "diễn viên ",
]

# Regex
_DATE_LIKE = re.compile(
    r"^(năm|tháng|ngày|tuần|giờ|phút|quý|thế\s*kỷ|thập\s*kỷ)\s*\d",
    re.IGNORECASE,
)
_PURE_NUMERIC = re.compile(r"^[\d\s,.\-+]+$")
_HAS_PERCENT = re.compile(r"[%‰]")
_CURRENCY = re.compile(r"(USD|VND|EUR|JPY|CNY|đ|đồng|tỷ|triệu|nghìn)\s*$", re.IGNORECASE)
_LEADING_PUNCT = re.compile(r"^[\s,.;:!?\-–—(){}[\]'\"“”‘’]+")
_TRAILING_PUNCT = re.compile(r"[\s,.;:!?\-–—(){}[\]'\"“”‘’]+$")
_MULTI_WS = re.compile(r"\s+")


# ====================================================================
# Core clean function
# ====================================================================
def normalize_text(text: str) -> str:
    """Strip punct đầu/cuối, chuẩn whitespace."""
    if not text:
        return ""
    t = text.strip()
    t = _LEADING_PUNCT.sub("", t)
    t = _TRAILING_PUNCT.sub("", t)
    t = _MULTI_WS.sub(" ", t).strip()
    return t


def strip_prefix_if_any(text: str, prefixes: List[str]) -> str:
    """Nếu text bắt đầu với prefix nào, bỏ prefix đó. Case-insensitive."""
    lower = text.lower()
    for p in prefixes:
        if lower.startswith(p):
            return text[len(p):].strip()
    return text


def has_any_prefix(text: str, prefixes: List[str]) -> bool:
    lower = text.lower()
    return any(lower.startswith(p) for p in prefixes)


def is_date_like(text: str) -> bool:
    return bool(_DATE_LIKE.match(text))


def is_pure_numeric(text: str) -> bool:
    return bool(_PURE_NUMERIC.match(text))


def is_currency_or_percent(text: str) -> bool:
    return bool(_HAS_PERCENT.search(text) or _CURRENCY.search(text))


def is_common_word(text: str) -> bool:
    return text.strip().lower() in COMMON_WORDS_REJECT


def clean_entity(entity: Dict[str, str]) -> Optional[Dict[str, str]]:
    """
    Clean 1 entity. Trả None nếu entity là noise, nên loại bỏ.
    Có thể đổi type nếu phát hiện prefix đặc biệt.
    """
    text = entity.get("text", "")
    etype = entity.get("type", "")
    if not text or not etype:
        return None

    # 1. Normalize text
    text = normalize_text(text)
    if not text:
        return None

    # 2. Length check
    if len(text) < MIN_ENTITY_LEN or len(text) > MAX_ENTITY_LEN:
        return None

    # 3. Pure numeric / date / percent / currency
    if is_pure_numeric(text):
        return None
    if is_date_like(text):
        return None
    if is_currency_or_percent(text):
        return None

    # 4. Common generic word
    if is_common_word(text):
        return None

    # 5. Re-classify & strip prefix
    # Nếu có admin LOC prefix → force type=LOC và strip
    if has_any_prefix(text, ADMIN_LOC_PREFIXES):
        stripped = strip_prefix_if_any(text, ADMIN_LOC_PREFIXES)
        # Sau strip phải còn ít nhất 2 chars
        if len(stripped) >= MIN_ENTITY_LEN:
            text = stripped
        etype = "LOC"  # force

    # Nếu có PER title prefix → force type=PER và strip
    elif has_any_prefix(text, PER_TITLE_PREFIXES):
        stripped = strip_prefix_if_any(text, PER_TITLE_PREFIXES)
        if len(stripped) >= MIN_ENTITY_LEN:
            text = stripped
        etype = "PER"

    # 6. Re-check length sau khi strip
    if len(text) < MIN_ENTITY_LEN or len(text) > MAX_ENTITY_LEN:
        return None

    # 7. Reject nếu là common word sau khi strip prefix
    if is_common_word(text):
        return None

    return {"text": text, "type": etype}


def dedup_entities(entities: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Dedup theo (text.lower(), type). Giữ thứ tự xuất hiện đầu tiên."""
    seen = set()
    out = []
    for e in entities:
        key = (e["text"].lower(), e["type"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def clean_record(record: Dict) -> Dict:
    """Clean entities của 1 record."""
    entities = record.get("entities", [])
    cleaned = []
    for e in entities:
        result = clean_entity(e)
        if result is not None:
            cleaned.append(result)
    record["entities"] = dedup_entities(cleaned)
    return record


# ====================================================================
# Pipeline
# ====================================================================
def run(input_patterns: List[str], output_path: str, verbose: bool = False):
    # 0. Xoá output partitions cũ (full rebuild — clean không cần incremental, 1-2 phút là xong)
    removed = clear_all_partitions(output_path)
    if removed:
        print(f"[0/2] Removed {removed} existing output partition(s)")

    # 1. Expand input + report
    inputs = expand_inputs(input_patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {input_patterns}", file=sys.stderr)
        sys.exit(1)
    print(f"[1/2] Cleaning entities from {len(inputs)} input file(s):")
    for p in inputs:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"        {p} ({size_mb:.1f} MB)")
    print(f"      Output → {output_path} (rotation at 89MB)")

    stats = Counter()
    type_before = Counter()
    type_after = Counter()
    total_ent_before = 0
    total_ent_after = 0
    records_written = 0

    # 2. Stream records từ mọi partition, clean, ghi với rotation
    print("[2/2] Processing...")
    with PartitionedJsonlWriter(output_path) as writer:
        for record in iter_records(inputs):
            ents_before = record.get("entities", [])
            total_ent_before += len(ents_before)
            for e in ents_before:
                type_before[e.get("type", "")] += 1

            record = clean_record(record)

            ents_after = record["entities"]
            total_ent_after += len(ents_after)
            for e in ents_after:
                type_after[e.get("type", "")] += 1

            stats["removed_entities"] += len(ents_before) - len(ents_after)

            writer.write(record)
            records_written += 1
            stats["records"] += 1

            if verbose and records_written <= 3:
                print(f"--- Sample {records_written} ---")
                print(f"Before: {len(ents_before)} entities")
                print(f"After : {len(ents_after)} entities")
                print(f"Sample after: {ents_after[:5]}")
                print()

    reduction_pct = (1 - total_ent_after / total_ent_before) * 100 if total_ent_before else 0
    print()
    print("=" * 60)
    print("CLEAN STATS")
    print("=" * 60)
    print(f"Records processed    : {records_written:,}")
    print(f"Entities before clean: {total_ent_before:,}")
    print(f"Entities after clean : {total_ent_after:,}")
    print(f"Removed              : {stats['removed_entities']:,} ({reduction_pct:.1f}% noise)")
    print(f"Avg entities/article : {total_ent_after / max(records_written,1):.1f}")
    print()
    print("By type BEFORE:")
    for t, c in type_before.most_common():
        print(f"  {t:<8} {c:>8,}")
    print()
    print("By type AFTER:")
    for t, c in type_after.most_common():
        delta = c - type_before.get(t, 0)
        print(f"  {t:<8} {c:>8,}  ({delta:+,})")
    print()
    print("Output partitions:")
    for p in all_partition_paths(output_path):
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p}: {size_mb:.1f} MB")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        action="append",
        default=None,
        help="Input pattern(s) (glob). Repeatable. Default: ../data/articles_ner*.jsonl",
    )
    ap.add_argument("--output", default="../data/articles_ready.jsonl")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    inputs = args.input or ["../data/articles_ner*.jsonl"]
    run(inputs, args.output, verbose=args.verbose)


if __name__ == "__main__":
    main()
