#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
02_index_to_es.py
-----------------
Bulk index JSONL vào Elasticsearch.

Flow:
1. Check ES health
2. (Optional) Drop + create index `news_articles` với mapping tối ưu
3. Install ICU analysis plugin (nếu chưa có)
4. Bulk index records theo batch 500
5. Tạo alias `news_public` với filter cho Kịch bản 3 RBAC:
   - Chỉ bài canonical
   - Ẩn field author (runtime field hoặc source filtering)
6. Refresh index + report stats

Usage:
    # Default: đọc ../data/articles_ready*.jsonl
    python 02_index_to_es.py --reset

    # Index mới (drop + create)
    python 02_index_to_es.py --input '../data/articles_ready*.jsonl' --reset

    # Append thêm (không drop)
    python 02_index_to_es.py --input '../data/articles_ready*.jsonl'

    # Với ES ở host khác
    python 02_index_to_es.py --es http://localhost:9200
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, Iterable, List

try:
    from elasticsearch import Elasticsearch, helpers
    from elasticsearch.exceptions import NotFoundError, RequestError
except ImportError:
    print("ERROR: Cài elasticsearch client: pip install 'elasticsearch>=8,<9'", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pre_dataset"))
from partition_io import expand_inputs, iter_records


# ==================================================================
# Config
# ==================================================================
INDEX_NAME = "news_articles"
ALIAS_PUBLIC = "news_public"   # cho guest role (Kịch bản 3)
DEFAULT_BATCH = 500


def load_mapping(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_icu_plugin(es: Elasticsearch):
    """Check xem ES có plugin analysis-icu không. Nếu không, dùng mapping fallback."""
    try:
        plugins = es.cat.plugins(format="json")
        plugin_names = {p["component"] for p in plugins}
        if "analysis-icu" not in plugin_names:
            print("⚠ ICU plugin not installed. Install với:")
            print("  docker exec news-es bin/elasticsearch-plugin install --batch analysis-icu")
            print("  docker restart news-es")
            print()
            return False
        return True
    except Exception as e:
        print(f"⚠ Could not check plugins: {e}")
        return False


def create_index(es: Elasticsearch, mapping_path: str, reset: bool = False):
    if es.indices.exists(index=INDEX_NAME):
        if reset:
            print(f"Deleting existing index {INDEX_NAME}...")
            es.indices.delete(index=INDEX_NAME)
        else:
            print(f"Index {INDEX_NAME} already exists, appending to it")
            return

    mapping = load_mapping(mapping_path)

    # Nếu không có ICU plugin, fallback sang standard analyzer
    has_icu = ensure_icu_plugin(es)
    if not has_icu:
        print("Falling back to standard analyzer (no ICU)")
        mapping["settings"]["analysis"] = {
            "analyzer": {
                "vi_text": {
                    "type": "standard",
                    "stopwords": "_none_"
                },
                "vi_text_search": {
                    "type": "standard",
                    "stopwords": "_none_"
                }
            }
        }

    print(f"Creating index {INDEX_NAME}...")
    es.indices.create(index=INDEX_NAME, **mapping)
    print(f"✓ Index {INDEX_NAME} created")


def create_alias(es: Elasticsearch):
    """Alias news_public cho guest role — chỉ trả bài canonical, ẩn author."""
    filter_q = {"term": {"is_canonical": True}}
    # Source filtering để ẩn author
    actions = [{
        "add": {
            "index": INDEX_NAME,
            "alias": ALIAS_PUBLIC,
            "filter": filter_q,
            "routing": None
        }
    }]
    es.indices.update_aliases(actions=actions)
    print(f"✓ Alias {ALIAS_PUBLIC} created (canonical only)")


def jsonl_generator(paths: List[str]) -> Iterable[Dict]:
    """Yield bulk actions từ multi-partition JSONL."""
    for doc in iter_records(paths):
        # ES reject nếu gặp unknown field (dynamic=strict)
        doc_cleaned = {k: v for k, v in doc.items() if k in ALLOWED_FIELDS}
        if "id" not in doc_cleaned:
            continue
        yield {
            "_op_type": "index",
            "_index": INDEX_NAME,
            "_id": doc_cleaned["id"],
            "_source": doc_cleaned,
        }


# Whitelist các field khớp với ES mapping (để tránh lỗi khi data có field dư thừa)
ALLOWED_FIELDS = {
    "id", "url", "title", "content",
    "published_at", "crawled_at", "publish_date", "publish_hour", "publish_dow",
    "source", "source_domain", "source_type",
    "category_raw", "category_normalized", "author", "language",
    "keywords", "entities",
    "content_length", "word_count", "has_full_content",
    "dup_group_id", "is_canonical", "dup_count",
}


def bulk_index(es: Elasticsearch, input_paths: List[str], batch_size: int):
    print(f"Bulk indexing {len(input_paths)} file(s) (batch={batch_size})...")
    for p in input_paths:
        size_mb = os.path.getsize(p) / 1024 / 1024
        print(f"  {p} ({size_mb:.1f} MB)")
    start = time.time()
    successes = 0
    failures = 0
    try:
        for ok, info in helpers.streaming_bulk(
            es,
            jsonl_generator(input_paths),
            chunk_size=batch_size,
            raise_on_error=False,
            request_timeout=60,
        ):
            if ok:
                successes += 1
                if successes % 1000 == 0:
                    rate = successes / (time.time() - start)
                    print(f"  [{successes:,}] @ {rate:.0f} docs/s")
            else:
                failures += 1
                if failures <= 5:
                    print(f"  ✗ {info}")
    except Exception as e:
        print(f"Bulk error: {e}")

    elapsed = time.time() - start
    print(f"\n✓ Indexed {successes:,} docs ({failures} failures) in {elapsed:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        action="append",
        default=None,
        help="Input pattern(s) (glob). Repeatable. Default: ../data/articles_ready*.jsonl",
    )
    ap.add_argument("--es", default="http://localhost:9200", help="ES URL")
    ap.add_argument(
        "--mapping",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "es_mapping.json"),
    )
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--reset", action="store_true",
                    help="Drop index trước khi tạo lại (xóa data cũ)")
    ap.add_argument("--skip-alias", action="store_true")
    args = ap.parse_args()

    input_patterns = args.input or ["../data/articles_ready*.jsonl"]
    inputs = expand_inputs(input_patterns)
    if not inputs:
        print(f"ERROR: no input files matched: {input_patterns}", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to ES: {args.es}")
    es = Elasticsearch(args.es, request_timeout=30)
    if not es.ping():
        print(f"✗ Cannot connect to {args.es}")
        sys.exit(1)
    info = es.info()
    print(f"✓ Connected — ES {info['version']['number']}")
    print()

    create_index(es, args.mapping, reset=args.reset)
    bulk_index(es, inputs, args.batch)

    # Refresh để doc visible ngay
    es.indices.refresh(index=INDEX_NAME)
    count = es.count(index=INDEX_NAME)["count"]
    print(f"\nIndex {INDEX_NAME} now has {count:,} docs")

    if not args.skip_alias:
        # Recreate alias (idempotent)
        try:
            es.indices.delete_alias(index=INDEX_NAME, name=ALIAS_PUBLIC)
        except NotFoundError:
            pass
        create_alias(es)
        pub_count = es.count(index=ALIAS_PUBLIC)["count"]
        print(f"Alias {ALIAS_PUBLIC} has {pub_count:,} docs (canonical only)")


if __name__ == "__main__":
    main()
