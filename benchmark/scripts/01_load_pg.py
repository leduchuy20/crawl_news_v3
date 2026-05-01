#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01_load_pg.py
-------------
Load JSONL vào PostgreSQL, rồi tạo index (post-insert để insert nhanh).

Hỗ trợ multi-partition input (glob) — dùng `articles_ready*.jsonl` để fair
với ES + ClickHouse (cùng dataset đã clean entities + lọc keyword category).

Usage:
    # Default (multi-partition): đọc ../data/articles_ready*.jsonl
    python 01_load_pg.py --reset

    # Explicit
    python 01_load_pg.py --input '../data/articles_ready*.jsonl' --reset
    python 01_load_pg.py --input ../data/articles_ready.jsonl \\
                        --input ../data/articles_ready_2026-05.jsonl --reset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# Re-use multi-partition helpers từ pre_dataset/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "pre_dataset"))
from partition_io import expand_inputs, iter_records  # noqa: E402

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


# ==================================================================
# Helpers (re-use logic từ 03_load_to_ch.py)
# ==================================================================
def parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def record_to_article_tuple(r: Dict) -> tuple:
    return (
        r.get("id", ""),
        r.get("url", ""),
        r.get("title", ""),
        r.get("content", ""),
        parse_datetime(r.get("published_at", "")),
        parse_datetime(r.get("crawled_at", "")),
        parse_date(r.get("publish_date", "")),
        int(r.get("publish_hour") or 0),
        int(r.get("publish_dow") or 0),
        r.get("source", ""),
        r.get("source_domain", ""),
        r.get("source_type", ""),
        r.get("category_raw", ""),
        r.get("category_normalized", ""),
        r.get("author", "") or None,
        r.get("language", "vi"),
        r.get("keywords") or [],
        json.dumps(r.get("entities") or [], ensure_ascii=False),  # JSONB
        int(r.get("content_length") or 0),
        int(r.get("word_count") or 0),
        bool(r.get("has_full_content")),
        r.get("dup_group_id", "") or None,
        bool(r.get("is_canonical")),
        int(r.get("dup_count") or 1),
    )


ARTICLE_COLS = (
    "id, url, title, content, published_at, crawled_at, "
    "publish_date, publish_hour, publish_dow, "
    "source, source_domain, source_type, category_raw, category_normalized, "
    "author, language, keywords, entities, "
    "content_length, word_count, has_full_content, "
    "dup_group_id, is_canonical, dup_count"
)
ARTICLE_PLACEHOLDERS = ",".join(["%s"] * 24)


# ==================================================================
# Main
# ==================================================================
def run_schema(cur, schema_path: str):
    with open(schema_path) as f:
        sql = f.read()
    cur.execute(sql)
    print("✓ Schema created")


def load_articles(cur, input_paths: List[str], batch_size: int = 1000):
    print("Loading articles...")
    buf = []
    total = 0
    start = time.time()
    insert_sql = f"INSERT INTO articles ({ARTICLE_COLS}) VALUES ({ARTICLE_PLACEHOLDERS})"

    for r in iter_records(input_paths):
        buf.append(record_to_article_tuple(r))
        if len(buf) >= batch_size:
            psycopg2.extras.execute_batch(cur, insert_sql, buf, page_size=batch_size)
            total += len(buf)
            rate = total / (time.time() - start)
            print(f"  [articles] {total:,} @ {rate:.0f}/s")
            buf.clear()
    if buf:
        psycopg2.extras.execute_batch(cur, insert_sql, buf, page_size=len(buf))
        total += len(buf)
    print(f"  [articles] DONE: {total:,} rows in {time.time()-start:.1f}s")
    return total


def load_keyword_events(cur, input_paths: List[str], batch_size: int = 5000):
    print("Loading keyword_events (fan-out)...")
    buf = []
    total = 0
    start = time.time()
    insert_sql = (
        "INSERT INTO keyword_events "
        "(article_id, keyword, publish_date, publish_hour, source, category_normalized) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    for r in iter_records(input_paths):
        pd = parse_date(r.get("publish_date", ""))
        if not pd:
            continue
        for kw in (r.get("keywords") or []):
            kw = kw.strip()
            if not kw:
                continue
            buf.append((
                r.get("id", ""),
                kw,
                pd,
                int(r.get("publish_hour") or 0),
                r.get("source", ""),
                r.get("category_normalized", ""),
            ))
            if len(buf) >= batch_size:
                psycopg2.extras.execute_batch(cur, insert_sql, buf, page_size=batch_size)
                total += len(buf)
                buf.clear()
    if buf:
        psycopg2.extras.execute_batch(cur, insert_sql, buf, page_size=len(buf))
        total += len(buf)
    print(f"  [keyword_events] DONE: {total:,} rows in {time.time()-start:.1f}s")
    return total


def load_entity_events(cur, input_paths: List[str], batch_size: int = 5000):
    print("Loading entity_events (fan-out)...")
    buf = []
    total = 0
    start = time.time()
    insert_sql = (
        "INSERT INTO entity_events "
        "(article_id, entity_text, entity_type, publish_date, source, category_normalized) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    for r in iter_records(input_paths):
        pd = parse_date(r.get("publish_date", ""))
        if not pd:
            continue
        for e in (r.get("entities") or []):
            txt = (e.get("text") or "").strip()
            etype = (e.get("type") or "").strip()
            if not txt or not etype:
                continue
            buf.append((
                r.get("id", ""),
                txt,
                etype,
                pd,
                r.get("source", ""),
                r.get("category_normalized", ""),
            ))
            if len(buf) >= batch_size:
                psycopg2.extras.execute_batch(cur, insert_sql, buf, page_size=batch_size)
                total += len(buf)
                buf.clear()
    if buf:
        psycopg2.extras.execute_batch(cur, insert_sql, buf, page_size=len(buf))
        total += len(buf)
    print(f"  [entity_events] DONE: {total:,} rows in {time.time()-start:.1f}s")
    return total


INDEXES = [
    # B-tree cho filter
    ("idx_articles_publish_date", "CREATE INDEX idx_articles_publish_date ON articles (publish_date)"),
    ("idx_articles_source",       "CREATE INDEX idx_articles_source ON articles (source)"),
    ("idx_articles_category",     "CREATE INDEX idx_articles_category ON articles (category_normalized)"),
    ("idx_articles_is_canonical", "CREATE INDEX idx_articles_is_canonical ON articles (is_canonical)"),
    # GIN cho FTS
    ("idx_articles_fts",          "CREATE INDEX idx_articles_fts ON articles USING GIN (fts)"),
    # GIN cho trigram
    ("idx_articles_title_trgm",   "CREATE INDEX idx_articles_title_trgm ON articles USING GIN (title gin_trgm_ops)"),
    ("idx_articles_content_trgm", "CREATE INDEX idx_articles_content_trgm ON articles USING GIN (content gin_trgm_ops)"),
    # JSONB path ops (nhanh hơn default cho @> operator)
    ("idx_articles_entities_gin", "CREATE INDEX idx_articles_entities_gin ON articles USING GIN (entities jsonb_path_ops)"),
    # Array
    ("idx_articles_keywords_gin", "CREATE INDEX idx_articles_keywords_gin ON articles USING GIN (keywords)"),
    # Aggregation
    ("idx_kwev_date_kw",          "CREATE INDEX idx_kwev_date_kw ON keyword_events (publish_date, keyword)"),
    ("idx_kwev_kw_cat",           "CREATE INDEX idx_kwev_kw_cat ON keyword_events (keyword, category_normalized)"),
    ("idx_entev_date_type",       "CREATE INDEX idx_entev_date_type ON entity_events (publish_date, entity_type)"),
]


def create_indexes(cur):
    print()
    print("Creating indexes (this is the slow part)...")
    for name, sql in INDEXES:
        t0 = time.time()
        print(f"  Creating {name}...", end=" ", flush=True)
        cur.execute(sql)
        print(f"{time.time()-t0:.1f}s")
    print("  Running VACUUM ANALYZE...")
    t0 = time.time()
    # VACUUM cần autocommit
    cur.execute("COMMIT")
    cur.execute("VACUUM ANALYZE")
    print(f"  VACUUM ANALYZE {time.time()-t0:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        action="append",
        default=None,
        help="Input pattern(s) (glob). Repeatable. Default: ../data/articles_ready*.jsonl",
    )
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--db", default="news_bench")
    ap.add_argument("--user", default="bench")
    ap.add_argument("--password", default="bench")
    ap.add_argument("--schema", default="config/pg_schema.sql")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--skip-schema", action="store_true")
    args = ap.parse_args()

    patterns = args.input or ["../data/articles_ready*.jsonl"]
    inputs = expand_inputs(patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {patterns}", file=sys.stderr)
        sys.exit(1)
    print(f"Input files ({len(inputs)} partition(s)):")
    for p in inputs:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p} ({size_mb:.1f} MB)")
    print()

    print(f"Connecting Postgres {args.host}:{args.port}/{args.db}...")
    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.db,
        user=args.user, password=args.password,
    )
    conn.set_session(autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT version()")
    print(f"✓ Connected to: {cur.fetchone()[0].split(',')[0]}")
    print()

    if args.reset or not args.skip_schema:
        run_schema(cur, args.schema)
        conn.commit()

    load_articles(cur, inputs)
    conn.commit()

    load_keyword_events(cur, inputs)
    conn.commit()

    load_entity_events(cur, inputs)
    conn.commit()

    create_indexes(cur)

    # Final counts
    print()
    print("=" * 60)
    print("FINAL COUNTS (Postgres)")
    print("=" * 60)
    for tbl in ["articles", "keyword_events", "entity_events"]:
        cur.execute(f"SELECT count(*) FROM {tbl}")
        c = cur.fetchone()[0]
        print(f"  {tbl:<20} {c:>10,}")

    # Size on disk
    print()
    print("Disk usage:")
    cur.execute("""
        SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
        FROM pg_catalog.pg_statio_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
        LIMIT 10
    """)
    for name, size in cur.fetchall():
        print(f"  {name:<25} {size}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
