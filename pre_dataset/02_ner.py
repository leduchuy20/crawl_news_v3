#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
02_ner.py
---------
Bước 2 — Named Entity Recognition với underthesea.

INCREMENTAL: chỉ chạy NER cho bài CHƯA có entities trong output hiện có.
Khi có dữ liệu crawl mới + cleanup xong → chạy lại script này, nó tự bỏ qua
các ID đã có trong `articles_ner.jsonl*` và chỉ NER phần còn lại.

Chạy NER trên TITLE + CONTENT của mỗi bài. Output: field `entities` dạng:
    [{"text": "TP.HCM", "type": "LOC"}, {"text": "Nguyễn Văn Yên", "type": "PER"}, ...]

Types theo VLSP tag set:
    PER = người
    LOC = địa điểm
    ORG = tổ chức
    MISC = các entity khác

Features:
- Multi-partition input (glob) + output rotation ở 89MB
- Resume sau crash (đọc ID đã có trong mọi output partition)
- Progress log mỗi 100 bài, ETA rate
- Multiprocessing optional (--workers N)
- Dedup entities cùng bài
- Batch flush mỗi 100 records

Usage:
    # Default: đọc articles_cleaned.jsonl*, ghi articles_ner.jsonl*
    python 02_ner.py

    # Multi-worker
    python 02_ner.py --workers 4

    # Chỉ chạy trên title (nhanh, test)
    python 02_ner.py --title-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from multiprocessing import Pool, cpu_count
from typing import Any, Dict, List, Tuple

try:
    from underthesea import ner
except ImportError:
    print("ERROR: underthesea chưa cài. Chạy: pip install underthesea", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from partition_io import (
    expand_inputs,
    iter_records,
    load_existing_ids,
    PartitionedJsonlWriter,
)


# ====================================================================
# Config
# ====================================================================
MIN_ENTITY_LEN = 2
MAX_ENTITY_LEN = 60
MAX_CONTENT_CHARS_FOR_NER = 5000  # Truncate content quá dài để NER không quá chậm


# ====================================================================
# NER extraction
# ====================================================================
def _extract_entities_from_ner_output(ner_output: List[Tuple[str, str, str, str]]) -> List[Dict[str, str]]:
    """
    underthesea.ner() trả về list tuple (token, pos_tag, chunk_tag, ner_tag).
    ner_tag dạng BIO: B-PER, I-PER, B-LOC, ...
    Gộp các token liên tiếp cùng entity thành 1 entity hoàn chỉnh.
    """
    entities = []
    current_tokens: List[str] = []
    current_type: str = ""

    for item in ner_output:
        if len(item) < 4:
            continue
        token = item[0]
        ner_tag = item[3]

        if ner_tag == "O":
            if current_tokens and current_type:
                text = " ".join(current_tokens).strip()
                if MIN_ENTITY_LEN <= len(text) <= MAX_ENTITY_LEN:
                    entities.append({"text": text, "type": current_type})
            current_tokens = []
            current_type = ""
        elif ner_tag.startswith("B-"):
            if current_tokens and current_type:
                text = " ".join(current_tokens).strip()
                if MIN_ENTITY_LEN <= len(text) <= MAX_ENTITY_LEN:
                    entities.append({"text": text, "type": current_type})
            current_type = ner_tag[2:]
            current_tokens = [token]
        elif ner_tag.startswith("I-"):
            if ner_tag[2:] == current_type:
                current_tokens.append(token)
            else:
                if current_tokens and current_type:
                    text = " ".join(current_tokens).strip()
                    if MIN_ENTITY_LEN <= len(text) <= MAX_ENTITY_LEN:
                        entities.append({"text": text, "type": current_type})
                current_type = ner_tag[2:]
                current_tokens = [token]

    if current_tokens and current_type:
        text = " ".join(current_tokens).strip()
        if MIN_ENTITY_LEN <= len(text) <= MAX_ENTITY_LEN:
            entities.append({"text": text, "type": current_type})

    return entities


def extract_entities(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    try:
        output = ner(text)
        return _extract_entities_from_ner_output(output)
    except Exception as e:
        print(f"  NER error on text (first 50 chars: {text[:50]!r}): {e}", file=sys.stderr)
        return []


def dedup_entities(entities: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for e in entities:
        key = (e["text"].lower(), e["type"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def process_record(record: Dict[str, Any], title_only: bool = False) -> Dict[str, Any]:
    title = record.get("title", "") or ""
    content = record.get("content", "") or ""

    all_entities: List[Dict[str, str]] = []

    if title:
        all_entities.extend(extract_entities(title))

    if not title_only and content:
        content_for_ner = content[:MAX_CONTENT_CHARS_FOR_NER]
        all_entities.extend(extract_entities(content_for_ner))

    record["entities"] = dedup_entities(all_entities)
    return record


def _worker_process(args: Tuple[str, bool]) -> str:
    """Worker IPC: nhận JSON string, trả JSON string đã populate entities."""
    json_str, title_only = args
    try:
        record = json.loads(json_str)
        record = process_record(record, title_only=title_only)
        return json.dumps(record, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write(f"Worker error: {e}\n{traceback.format_exc()}")
        return json_str


# ====================================================================
# Pipeline — multi-partition IO, incremental
# ====================================================================
def run_ner_pipeline(
    input_patterns: List[str],
    output_path: str,
    workers: int = 1,
    title_only: bool = False,
    flush_every: int = 100,
):
    # Expand inputs
    inputs = expand_inputs(input_patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {input_patterns}", file=sys.stderr)
        sys.exit(1)

    print(f"Input patterns : {input_patterns}")
    print(f"Input files    : {len(inputs)}")
    for p in inputs:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p} ({size_mb:.1f} MB)")
    print(f"Output         : {output_path} (rotation at 89MB)")
    print(f"Mode           : {'TITLE only' if title_only else 'TITLE + CONTENT'}")
    print(f"Workers        : {workers}")

    # Incremental: load ID đã có entities từ mọi output partition
    print("Scanning existing output for done IDs...")
    done_ids = load_existing_ids(output_path)
    print(f"Already done   : {len(done_ids):,}")

    # Collect pending records (skip những bài đã có entities)
    print("Collecting pending records...")
    pending: List[Dict[str, Any]] = []
    total_input = 0
    for rec in iter_records(inputs):
        total_input += 1
        rid = rec.get("id")
        if not rid:
            continue
        if rid in done_ids:
            continue
        pending.append(rec)

    print(f"Total input    : {total_input:,}")
    print(f"Remaining      : {len(pending):,}")
    print()

    if not pending:
        print("Nothing to do — all records already processed.")
        return

    start_time = time.time()
    processed = 0

    with PartitionedJsonlWriter(output_path) as writer:
        if workers > 1:
            print(f"Processing with {workers} workers...")
            pending_json = [(json.dumps(r, ensure_ascii=False), title_only) for r in pending]
            with Pool(processes=workers) as pool:
                for i, result_json in enumerate(pool.imap_unordered(_worker_process, pending_json, chunksize=10), 1):
                    try:
                        writer.write(json.loads(result_json))
                    except json.JSONDecodeError:
                        continue
                    if i % flush_every == 0:
                        writer.flush()
                        elapsed = time.time() - start_time
                        rate = i / elapsed
                        eta_sec = (len(pending) - i) / rate if rate > 0 else 0
                        print(f"  [{i:,}/{len(pending):,}] rate={rate:.1f}/s ETA={eta_sec/60:.1f}min")
                    processed = i
        else:
            print("Running sequential...")
            for record in pending:
                record = process_record(record, title_only=title_only)
                writer.write(record)
                processed += 1
                if processed % flush_every == 0:
                    writer.flush()
                    elapsed = time.time() - start_time
                    rate = processed / elapsed
                    eta_sec = (len(pending) - processed) / rate if rate > 0 else 0
                    print(f"  [{processed:,}/{len(pending):,}] rate={rate:.1f}/s ETA={eta_sec/60:.1f}min")

    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print(f"NER DONE: {processed:,} records in {elapsed/60:.1f} min")
    print(f"Output base: {output_path}")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser(description="Run NER on cleaned multi-partition JSONL")
    ap.add_argument("--input", action="append", default=None,
                    help="Input pattern (glob). Có thể dùng nhiều lần. "
                         "Default: data/articles_cleaned*.jsonl")
    ap.add_argument("--output", default="data/articles_ner.jsonl",
                    help="Base output path (default: data/articles_ner.jsonl)")
    ap.add_argument("--workers", type=int, default=1,
                    help="Số process song song (default: 1)")
    ap.add_argument("--title-only", action="store_true",
                    help="Chỉ chạy NER trên title (nhanh, test)")
    ap.add_argument("--flush-every", type=int, default=100,
                    help="Flush output mỗi N record (default: 100)")
    args = ap.parse_args()

    if args.workers > cpu_count():
        print(f"Warning: --workers {args.workers} > CPU count {cpu_count()}")

    inputs = args.input or ["data/articles_cleaned*.jsonl"]
    run_ner_pipeline(
        input_patterns=inputs,
        output_path=args.output,
        workers=args.workers,
        title_only=args.title_only,
        flush_every=args.flush_every,
    )


if __name__ == "__main__":
    main()
