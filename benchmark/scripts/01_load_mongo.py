#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01_load_mongo.py
----------------
Load JSONL vào MongoDB, rồi tạo index. Mirror schema của Postgres để fair so
sánh storage (cùng dataset → cùng 3 collection: articles, keyword_events,
entity_events; cùng tập index tương đương).

Usage:
    python 01_load_mongo.py --reset
    python 01_load_mongo.py --input '../data/articles_ready*.jsonl' --reset
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

# Re-use multi-partition helpers từ pre_dataset/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "pre_dataset"))
from partition_io import expand_inputs, iter_records  # noqa: E402

try:
    from pymongo import MongoClient, ASCENDING, TEXT
    from pymongo.errors import BulkWriteError
except ImportError:
    print("ERROR: pip install pymongo", file=sys.stderr)
    sys.exit(1)


# ==================================================================
# Helpers
# ==================================================================
def parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_date(s: str) -> Optional[datetime]:
    """Mongo không có DATE riêng, dùng datetime ở 00:00."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


def record_to_article_doc(r: Dict) -> Dict:
    """JSONL record → MongoDB doc. _id = id để có unique constraint miễn phí."""
    return {
        "_id": r.get("id", ""),
        "url": r.get("url", ""),
        "title": r.get("title", ""),
        "content": r.get("content", ""),
        "published_at": parse_datetime(r.get("published_at", "")),
        "crawled_at": parse_datetime(r.get("crawled_at", "")),
        "publish_date": parse_date(r.get("publish_date", "")),
        "publish_hour": int(r.get("publish_hour") or 0),
        "publish_dow": int(r.get("publish_dow") or 0),
        "source": r.get("source", ""),
        "source_domain": r.get("source_domain", ""),
        "source_type": r.get("source_type", ""),
        "category_raw": r.get("category_raw", ""),
        "category_normalized": r.get("category_normalized", ""),
        "author": r.get("author", "") or None,
        "language": r.get("language", "vi"),
        "keywords": r.get("keywords") or [],
        "entities": r.get("entities") or [],
        "content_length": int(r.get("content_length") or 0),
        "word_count": int(r.get("word_count") or 0),
        "has_full_content": bool(r.get("has_full_content")),
        "dup_group_id": r.get("dup_group_id", "") or None,
        "is_canonical": bool(r.get("is_canonical")),
        "dup_count": int(r.get("dup_count") or 1),
    }


# ==================================================================
# Loaders
# ==================================================================
def load_articles(coll, input_paths: List[str], batch_size: int = 1000):
    print("Loading articles...")
    buf: List[Dict] = []
    total = 0
    start = time.time()

    def flush():
        nonlocal total, buf
        if not buf:
            return
        try:
            # ordered=False → tiếp tục dù gặp dup _id (resume-friendly)
            coll.insert_many(buf, ordered=False)
        except BulkWriteError as e:
            # Bỏ qua duplicate-key, đếm số doc thực sự insert
            dup = sum(1 for w in e.details.get("writeErrors", []) if w.get("code") == 11000)
            print(f"    (skipped {dup} duplicate _id)")
        total += len(buf)
        buf = []

    for r in iter_records(input_paths):
        buf.append(record_to_article_doc(r))
        if len(buf) >= batch_size:
            flush()
            rate = total / (time.time() - start)
            print(f"  [articles] {total:,} @ {rate:.0f}/s")
    flush()
    print(f"  [articles] DONE: {total:,} docs in {time.time()-start:.1f}s")
    return total


def load_keyword_events(coll, input_paths: List[str], batch_size: int = 5000):
    print("Loading keyword_events (fan-out)...")
    buf: List[Dict] = []
    total = 0
    start = time.time()

    def flush():
        nonlocal total, buf
        if not buf:
            return
        coll.insert_many(buf, ordered=False)
        total += len(buf)
        buf = []

    for r in iter_records(input_paths):
        pd = parse_date(r.get("publish_date", ""))
        if not pd:
            continue
        article_id = r.get("id", "")
        source = r.get("source", "")
        category = r.get("category_normalized", "")
        hour = int(r.get("publish_hour") or 0)
        for kw in (r.get("keywords") or []):
            kw = kw.strip()
            if not kw:
                continue
            buf.append({
                "article_id": article_id,
                "keyword": kw,
                "publish_date": pd,
                "publish_hour": hour,
                "source": source,
                "category_normalized": category,
            })
            if len(buf) >= batch_size:
                flush()
    flush()
    print(f"  [keyword_events] DONE: {total:,} docs in {time.time()-start:.1f}s")
    return total


def load_entity_events(coll, input_paths: List[str], batch_size: int = 5000):
    print("Loading entity_events (fan-out)...")
    buf: List[Dict] = []
    total = 0
    start = time.time()

    def flush():
        nonlocal total, buf
        if not buf:
            return
        coll.insert_many(buf, ordered=False)
        total += len(buf)
        buf = []

    for r in iter_records(input_paths):
        pd = parse_date(r.get("publish_date", ""))
        if not pd:
            continue
        article_id = r.get("id", "")
        source = r.get("source", "")
        category = r.get("category_normalized", "")
        for e in (r.get("entities") or []):
            txt = (e.get("text") or "").strip()
            etype = (e.get("type") or "").strip()
            if not txt or not etype:
                continue
            buf.append({
                "article_id": article_id,
                "entity_text": txt,
                "entity_type": etype,
                "publish_date": pd,
                "source": source,
                "category_normalized": category,
            })
            if len(buf) >= batch_size:
                flush()
    flush()
    print(f"  [entity_events] DONE: {total:,} docs in {time.time()-start:.1f}s")
    return total


# ==================================================================
# Indexes — tương đương với PG (cùng số lượng + chức năng)
# ==================================================================
def create_indexes(db):
    print()
    print("Creating indexes (this is the slow part)...")

    articles = db.articles
    kw_ev = db.keyword_events
    ent_ev = db.entity_events

    # Mỗi entry: (collection, keys, name, extra_kwargs)
    plan = [
        # ----- articles -----
        # B-tree-ish single field (BTree trong Mongo)
        (articles, [("publish_date", ASCENDING)],        "idx_publish_date", {}),
        (articles, [("source", ASCENDING)],              "idx_source", {}),
        (articles, [("category_normalized", ASCENDING)], "idx_category", {}),
        (articles, [("is_canonical", ASCENDING)],        "idx_is_canonical", {}),
        # Text index cho full-text (tương đương GIN(fts) của PG).
        # Mongo KHÔNG hỗ trợ tiếng Việt làm stemmer → phải:
        #   default_language='none'      → không stem (tokenize đơn giản theo whitespace)
        #   language_override='__lang__' → không đọc field `language` của doc
        #     (nếu không set, Mongo thấy doc.language='vi' → fail Location17262)
        (articles, [("title", TEXT), ("content", TEXT)], "idx_fts",
            {"default_language": "none", "language_override": "__lang__"}),
        # Multikey trên array → tương đương GIN(keywords) của PG
        (articles, [("keywords", ASCENDING)],            "idx_keywords", {}),
        # Embedded doc field — tương đương GIN(entities) của PG
        (articles, [("entities.text", ASCENDING),
                    ("entities.type", ASCENDING)],       "idx_entities", {}),
        # ----- keyword_events -----
        (kw_ev, [("publish_date", ASCENDING),
                 ("keyword", ASCENDING)],                "idx_date_kw", {}),
        (kw_ev, [("keyword", ASCENDING),
                 ("category_normalized", ASCENDING)],    "idx_kw_cat", {}),
        # ----- entity_events -----
        (ent_ev, [("publish_date", ASCENDING),
                  ("entity_type", ASCENDING)],           "idx_date_type", {}),
    ]

    for coll, keys, name, extra in plan:
        t0 = time.time()
        print(f"  Creating {coll.name}.{name}...", end=" ", flush=True)
        coll.create_index(keys, name=name, background=False, **extra)
        print(f"{time.time()-t0:.1f}s")


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
    ap.add_argument("--port", type=int, default=27017)
    ap.add_argument("--user", default="bench")
    ap.add_argument("--password", default="bench")
    ap.add_argument("--auth-db", default="admin")
    ap.add_argument("--db", default="news_bench")
    ap.add_argument("--reset", action="store_true",
                    help="Drop 3 collections trước khi load")
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

    print(f"Connecting MongoDB {args.host}:{args.port}/{args.db}...")
    client = MongoClient(
        host=args.host, port=args.port,
        username=args.user, password=args.password,
        authSource=args.auth_db,
        serverSelectionTimeoutMS=10000,
    )
    info = client.server_info()
    print(f"✓ Connected to MongoDB {info.get('version', '?')}")
    db = client[args.db]

    if args.reset:
        print("DROP collections...")
        for name in ("articles", "keyword_events", "entity_events"):
            db.drop_collection(name)
        print("✓ Dropped")

    start = time.time()
    n_art = load_articles(db.articles, inputs)
    n_kw = load_keyword_events(db.keyword_events, inputs)
    n_ent = load_entity_events(db.entity_events, inputs)
    create_indexes(db)
    elapsed = time.time() - start
    print()
    print(f"✓ DONE in {elapsed:.1f}s")
    print(f"  articles        : {n_art:,}")
    print(f"  keyword_events  : {n_kw:,}")
    print(f"  entity_events   : {n_ent:,}")


if __name__ == "__main__":
    main()
