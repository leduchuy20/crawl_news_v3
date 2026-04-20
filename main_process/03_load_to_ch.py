#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
03_load_to_ch.py
----------------
Load JSONL vào ClickHouse:
1. Chạy `config/clickhouse_schema.sql` để tạo bảng
2. Bulk insert bảng `articles` (1 row/article)
3. Bulk insert bảng `keyword_events` (1 row per article × keyword)
4. Bulk insert bảng `entity_events` (1 row per article × entity)
5. Materialized views tự tính (news.hourly_keyword_stats, daily_*, ...)

Usage:
    # Default: đọc ../data/articles_ready*.jsonl
    python 03_load_to_ch.py --reset

    # Reset schema + load
    python 03_load_to_ch.py --input '../data/articles_ready*.jsonl' --reset

    # Chỉ load data (không recreate schema)
    python 03_load_to_ch.py --input '../data/articles_ready*.jsonl'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, Iterable, List, Tuple

try:
    from clickhouse_driver import Client as CHClient
except ImportError:
    print("ERROR: Cài: pip install clickhouse-driver", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pre_dataset"))
from partition_io import expand_inputs, iter_records


# ==================================================================
# Schema
# ==================================================================
ARTICLE_COLS = [
    "id", "url", "title", "content",
    "published_at", "crawled_at", "publish_date", "publish_hour", "publish_dow",
    "source", "source_domain", "source_type",
    "category_raw", "category_normalized", "author", "language",
    "content_length", "word_count",
    "has_full_content", "is_canonical", "dup_group_id", "dup_count",
    "keywords", "entity_texts", "entity_types",
]

KEYWORD_EVENT_COLS = [
    "article_id", "keyword", "event_time",
    "publish_date", "publish_hour",
    "source", "category_normalized",
]

ENTITY_EVENT_COLS = [
    "article_id", "entity_text", "entity_type", "event_time",
    "publish_date", "source", "category_normalized",
]


# ==================================================================
# Parsers
# ==================================================================
def parse_datetime(s: str) -> datetime:
    """Parse ISO string → naive datetime UTC (ClickHouse DateTime64 native)."""
    if not s:
        return datetime(1970, 1, 1)
    # Xử lý "Z" và timezone offset
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return datetime(1970, 1, 1)
    if dt.tzinfo is not None:
        # Convert về UTC rồi strip tzinfo (ClickHouse driver expect naive for tz-aware cols)
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_date(s: str):
    """Parse YYYY-MM-DD string → date object."""
    if not s:
        return datetime(1970, 1, 1).date()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return datetime(1970, 1, 1).date()


def record_to_article_row(r: Dict) -> List:
    """Chuyển 1 JSON record thành tuple phù hợp với bảng articles."""
    entities = r.get("entities", []) or []
    entity_texts = [e.get("text", "") for e in entities]
    entity_types = [e.get("type", "") for e in entities]

    return [
        r.get("id", ""),
        r.get("url", ""),
        r.get("title", ""),
        r.get("content", ""),
        parse_datetime(r.get("published_at", "")),
        parse_datetime(r.get("crawled_at", "")),
        parse_date(r.get("publish_date", "")),
        max(0, int(r.get("publish_hour") or 0)) if r.get("publish_hour", -1) >= 0 else 0,
        max(0, int(r.get("publish_dow") or 0)) if r.get("publish_dow", -1) >= 0 else 0,
        r.get("source", ""),
        r.get("source_domain", ""),
        r.get("source_type", ""),
        r.get("category_raw", ""),
        r.get("category_normalized", ""),
        r.get("author", ""),
        r.get("language", "vi"),
        int(r.get("content_length") or 0),
        int(r.get("word_count") or 0),
        1 if r.get("has_full_content") else 0,
        1 if r.get("is_canonical") else 0,
        r.get("dup_group_id", "") or "",
        int(r.get("dup_count") or 1),
        r.get("keywords", []) or [],
        entity_texts,
        entity_types,
    ]


def record_to_keyword_rows(r: Dict) -> List[List]:
    """Fan-out: 1 article × N keywords → N rows."""
    pub_dt = parse_datetime(r.get("published_at", ""))
    pub_date = parse_date(r.get("publish_date", ""))
    pub_hour = max(0, int(r.get("publish_hour") or 0)) if r.get("publish_hour", -1) >= 0 else 0
    aid = r.get("id", "")
    src = r.get("source", "")
    cat = r.get("category_normalized", "")
    rows = []
    for kw in (r.get("keywords") or []):
        if not kw or not kw.strip():
            continue
        rows.append([aid, kw.strip(), pub_dt, pub_date, pub_hour, src, cat])
    return rows


def record_to_entity_rows(r: Dict) -> List[List]:
    """Fan-out: 1 article × N entities → N rows."""
    pub_dt = parse_datetime(r.get("published_at", ""))
    pub_date = parse_date(r.get("publish_date", ""))
    aid = r.get("id", "")
    src = r.get("source", "")
    cat = r.get("category_normalized", "")
    rows = []
    for e in (r.get("entities") or []):
        txt = (e.get("text") or "").strip()
        etype = (e.get("type") or "").strip()
        if not txt or not etype:
            continue
        rows.append([aid, txt, etype, pub_dt, pub_date, src, cat])
    return rows


# ==================================================================
# Batched inserter
# ==================================================================
def batched_insert(client: CHClient, table: str, cols: List[str], row_iter, batch_size: int = 5000):
    """Insert rows theo batch để không OOM."""
    buffer = []
    total = 0
    start = time.time()
    insert_sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES"
    for row in row_iter:
        buffer.append(row)
        if len(buffer) >= batch_size:
            client.execute(insert_sql, buffer)
            total += len(buffer)
            elapsed = time.time() - start
            rate = total / max(elapsed, 1e-6)
            print(f"  [{table}] {total:,} @ {rate:.0f}/s")
            buffer = []
    if buffer:
        client.execute(insert_sql, buffer)
        total += len(buffer)
    print(f"  [{table}] DONE: {total:,} rows in {time.time()-start:.1f}s")
    return total


# ==================================================================
# Schema setup
# ==================================================================
def run_schema(client: CHClient, sql_path: str):
    """Execute SQL statements from schema file."""
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()

    # Split ra các statement (ClickHouse driver execute 1 statement/call)
    statements = []
    current = []
    for line in sql.splitlines():
        stripped = line.strip()
        # Skip pure comment lines
        if stripped.startswith("--") or not stripped:
            if current:
                current.append(line)
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
    if current:
        stmt = "\n".join(current).strip()
        if stmt:
            statements.append(stmt)

    print(f"Running {len(statements)} schema statements...")
    for i, stmt in enumerate(statements, 1):
        # Lấy 60 char đầu để log
        preview = " ".join(stmt.split())[:70]
        try:
            client.execute(stmt)
            print(f"  [{i}/{len(statements)}] ✓ {preview}...")
        except Exception as e:
            print(f"  [{i}/{len(statements)}] ✗ {preview}...")
            print(f"      ERROR: {e}")
            raise


# ==================================================================
# Generators
# ==================================================================
def article_rows_gen(paths: List[str]):
    for r in iter_records(paths):
        yield record_to_article_row(r)


def keyword_rows_gen(paths: List[str]):
    for r in iter_records(paths):
        for row in record_to_keyword_rows(r):
            yield row


def entity_rows_gen(paths: List[str]):
    for r in iter_records(paths):
        for row in record_to_entity_rows(r):
            yield row


# ==================================================================
# Main
# ==================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        action="append",
        default=None,
        help="Input pattern(s) (glob). Repeatable. Default: ../data/articles_ready*.jsonl",
    )
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--user", default="default")
    ap.add_argument("--password", default="")
    ap.add_argument(
        "--schema",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "clickhouse_schema.sql"),
    )
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--reset", action="store_true",
                    help="Drop + recreate schema trước khi load")
    ap.add_argument("--skip-schema", action="store_true",
                    help="Bỏ qua bước tạo schema (dùng khi schema đã có)")
    args = ap.parse_args()

    input_patterns = args.input or ["../data/articles_ready*.jsonl"]
    inputs = expand_inputs(input_patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {input_patterns}", file=sys.stderr)
        sys.exit(1)
    print(f"Loading from {len(inputs)} input file(s):")
    for p in inputs:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p} ({size_mb:.1f} MB)")
    print()

    print(f"Connecting to ClickHouse {args.host}:{args.port}...")
    client = CHClient(
        host=args.host, port=args.port, user=args.user, password=args.password,
        database="default", settings={"use_numpy": False},
    )
    version = client.execute("SELECT version()")[0][0]
    print(f"✓ Connected — ClickHouse {version}")
    print()

    # Schema
    if args.reset or not args.skip_schema:
        run_schema(client, args.schema)
        print()

    # Load articles
    print("Loading articles table...")
    batched_insert(
        client, "news.articles", ARTICLE_COLS,
        article_rows_gen(inputs), args.batch,
    )
    print()

    # Load keyword_events
    print("Loading keyword_events table...")
    batched_insert(
        client, "news.keyword_events", KEYWORD_EVENT_COLS,
        keyword_rows_gen(inputs), args.batch,
    )
    print()

    # Load entity_events
    print("Loading entity_events table...")
    batched_insert(
        client, "news.entity_events", ENTITY_EVENT_COLS,
        entity_rows_gen(inputs), args.batch,
    )
    print()

    # Counts report
    print("=" * 60)
    print("FINAL COUNTS")
    print("=" * 60)
    for tbl in ["news.articles", "news.keyword_events", "news.entity_events",
                "news.hourly_keyword_stats", "news.daily_keyword_stats",
                "news.daily_entity_stats"]:
        try:
            c = client.execute(f"SELECT count() FROM {tbl}")[0][0]
            print(f"  {tbl:<35} {c:>10,}")
        except Exception as e:
            print(f"  {tbl:<35} error: {e}")
    print("=" * 60)


if __name__ == "__main__":
    main()
