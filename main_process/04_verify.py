#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_verify.py
------------
Verify infrastructure + demo các query theo 3 kịch bản đề cương.

Chạy script này SAU khi đã:
1. docker-compose up -d
2. python scripts/02_index_to_es.py --input articles_final.jsonl --reset
3. python scripts/03_load_to_ch.py --input articles_final.jsonl --reset

Script này test:
✓ Health check ES + CH
✓ Kịch bản 1 (Search): full-text search, tìm theo keyword, entity
✓ Kịch bản 2 (Trending): top keywords theo ngày, so sánh tuần
✓ Kịch bản 3 (RBAC): so sánh kết quả giữa index gốc vs alias news_public

Usage:
    python 04_verify.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List

try:
    from elasticsearch import Elasticsearch
    from clickhouse_driver import Client as CHClient
except ImportError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)


# =================================================================
def print_header(text: str):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def print_section(text: str):
    print(f"\n--- {text} ---")


# =================================================================
# HEALTH CHECKS
# =================================================================
def check_es(es: Elasticsearch):
    print_section("Elasticsearch health")
    if not es.ping():
        print("✗ Cannot connect")
        return False
    info = es.info()
    print(f"✓ ES {info['version']['number']}")
    health = es.cluster.health()
    print(f"  cluster: {health['status']} ({health['number_of_nodes']} nodes)")
    try:
        c = es.count(index="news_articles")["count"]
        print(f"  news_articles: {c:,} docs")
    except Exception as e:
        print(f"  news_articles: ERROR — {e}")
        return False
    try:
        c2 = es.count(index="news_public")["count"]
        print(f"  news_public alias: {c2:,} docs")
    except Exception as e:
        print(f"  news_public: ERROR — {e}")
    return True


def check_ch(ch: CHClient):
    print_section("ClickHouse health")
    try:
        v = ch.execute("SELECT version()")[0][0]
        print(f"✓ ClickHouse {v}")
    except Exception as e:
        print(f"✗ {e}")
        return False
    for tbl in ["news.articles", "news.keyword_events", "news.entity_events",
                "news.daily_keyword_stats", "news.daily_entity_stats"]:
        try:
            c = ch.execute(f"SELECT count() FROM {tbl}")[0][0]
            print(f"  {tbl:<35} {c:>10,}")
        except Exception as e:
            print(f"  {tbl} ERROR — {e}")
    return True


# =================================================================
# KỊCH BẢN 1: SEARCH
# =================================================================
def demo_kb1_fulltext_search(es: Elasticsearch, query: str = "Iran"):
    print_section(f"KB1.a — Full-text search: '{query}'")
    t0 = time.time()
    res = es.search(
        index="news_articles",
        query={
            "multi_match": {
                "query": query,
                "fields": ["title^3", "content"],
                "type": "best_fields",
            }
        },
        size=5,
        highlight={
            "fields": {"title": {}, "content": {"fragment_size": 100, "number_of_fragments": 1}}
        },
        _source=["id", "title", "source", "publish_date", "category_normalized"],
    )
    elapsed_ms = (time.time() - t0) * 1000
    hits = res["hits"]["total"]["value"]
    print(f"  Matched: {hits:,} docs in {elapsed_ms:.1f} ms")
    for h in res["hits"]["hits"][:5]:
        s = h["_source"]
        snippet = ""
        if "highlight" in h and "content" in h["highlight"]:
            snippet = h["highlight"]["content"][0].replace("\n", " ")[:100]
        print(f"  • [{s['source']}/{s['category_normalized']}] {s['title'][:70]}")
        if snippet:
            print(f"      ...{snippet}...")


def demo_kb1_entity_search(es: Elasticsearch, entity_text: str = "Iran", entity_type: str = "LOC"):
    print_section(f"KB1.b — Entity-based search: {entity_type}='{entity_text}'")
    t0 = time.time()
    res = es.search(
        index="news_articles",
        query={
            "nested": {
                "path": "entities",
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"entities.text": entity_text}},
                            {"term": {"entities.type": entity_type}},
                        ]
                    }
                }
            }
        },
        size=5,
        _source=["id", "title", "source", "publish_date"],
    )
    elapsed_ms = (time.time() - t0) * 1000
    hits = res["hits"]["total"]["value"]
    print(f"  Matched: {hits:,} docs in {elapsed_ms:.1f} ms")
    for h in res["hits"]["hits"][:3]:
        s = h["_source"]
        print(f"  • [{s['source']}] {s['title'][:80]}")


def demo_kb1_combined(es: Elasticsearch, text: str = "tấn công", entity: str = "Iran"):
    print_section(f"KB1.c — Combined: text='{text}' + entity LOC='{entity}'")
    t0 = time.time()
    res = es.search(
        index="news_articles",
        query={
            "bool": {
                "must": [
                    {"match": {"content": text}},
                    {"nested": {
                        "path": "entities",
                        "query": {
                            "bool": {
                                "must": [
                                    {"term": {"entities.text": entity}},
                                    {"term": {"entities.type": "LOC"}},
                                ]
                            }
                        }
                    }},
                ]
            }
        },
        size=3,
        _source=["title", "source", "publish_date"],
    )
    elapsed_ms = (time.time() - t0) * 1000
    hits = res["hits"]["total"]["value"]
    print(f"  Matched: {hits:,} docs in {elapsed_ms:.1f} ms")
    for h in res["hits"]["hits"][:3]:
        s = h["_source"]
        print(f"  • [{s['source']}/{s['publish_date']}] {s['title'][:80]}")


# =================================================================
# KỊCH BẢN 2: TRENDING (ClickHouse)
# =================================================================
def demo_kb2_top_keywords(ch: CHClient, days: int = 7, category: str = None):
    cat_filter = f"AND category_normalized = '{category}'" if category else ""
    label = f"last {days}d" + (f" in {category}" if category else "")
    print_section(f"KB2.a — Top keywords ({label})")
    t0 = time.time()
    rows = ch.execute(f"""
        SELECT keyword, sum(mention_count) AS c
        FROM news.daily_keyword_stats
        WHERE publish_date >= today() - {days}
        {cat_filter}
        GROUP BY keyword
        ORDER BY c DESC
        LIMIT 10
    """)
    elapsed_ms = (time.time() - t0) * 1000
    print(f"  Query time: {elapsed_ms:.1f} ms")
    for kw, c in rows:
        print(f"    {c:>5}  {kw}")


def demo_kb2_hot_keywords(ch: CHClient, window_days: int = 7):
    """Tìm keyword tăng đột biến so với kỳ trước (spike detection)."""
    print_section(f"KB2.b — HOT keywords (spike last {window_days}d vs previous {window_days}d)")
    t0 = time.time()
    rows = ch.execute(f"""
        WITH
            current_window AS (
                SELECT keyword, sum(mention_count) AS cnt_now
                FROM news.daily_keyword_stats
                WHERE publish_date >= today() - {window_days}
                GROUP BY keyword
            ),
            prev_window AS (
                SELECT keyword, sum(mention_count) AS cnt_prev
                FROM news.daily_keyword_stats
                WHERE publish_date >= today() - {window_days * 2}
                  AND publish_date < today() - {window_days}
                GROUP BY keyword
            )
        SELECT
            c.keyword,
            c.cnt_now,
            coalesce(p.cnt_prev, 0) AS cnt_prev,
            round(c.cnt_now / greatest(p.cnt_prev, 1), 2) AS multiplier
        FROM current_window c
        LEFT JOIN prev_window p ON c.keyword = p.keyword
        WHERE c.cnt_now >= 5
        ORDER BY multiplier DESC, c.cnt_now DESC
        LIMIT 10
    """)
    elapsed_ms = (time.time() - t0) * 1000
    print(f"  Query time: {elapsed_ms:.1f} ms")
    print(f"  {'keyword':<40} {'now':>6} {'prev':>6} {'x':>8}")
    for kw, now_c, prev_c, mult in rows:
        print(f"    {kw[:38]:<38} {now_c:>6} {prev_c:>6} {mult:>8}")


def demo_kb2_hourly_distribution(ch: CHClient):
    print_section("KB2.c — Article volume by hour of day (last 30d)")
    t0 = time.time()
    rows = ch.execute("""
        SELECT publish_hour, count() AS c
        FROM news.articles
        WHERE publish_date >= today() - 30
        GROUP BY publish_hour
        ORDER BY publish_hour
    """)
    elapsed_ms = (time.time() - t0) * 1000
    print(f"  Query time: {elapsed_ms:.1f} ms")
    max_c = max((r[1] for r in rows), default=1)
    for hr, c in rows:
        bar = "█" * int(c / max_c * 30)
        print(f"    {hr:02d}h  {c:>5}  {bar}")


def demo_kb2_top_entities(ch: CHClient, entity_type: str = "LOC", days: int = 7):
    print_section(f"KB2.d — Top {entity_type} entities (last {days}d)")
    t0 = time.time()
    rows = ch.execute(f"""
        SELECT entity_text, sum(mention_count) AS c
        FROM news.daily_entity_stats
        WHERE publish_date >= today() - {days}
          AND entity_type = '{entity_type}'
        GROUP BY entity_text
        ORDER BY c DESC
        LIMIT 10
    """)
    elapsed_ms = (time.time() - t0) * 1000
    print(f"  Query time: {elapsed_ms:.1f} ms")
    for txt, c in rows:
        print(f"    {c:>5}  {txt}")


def demo_kb2_cross_source_coverage(ch: CHClient, days: int = 7):
    """Sự kiện hot: được nhiều nguồn đưa tin trong cùng 1 ngày."""
    print_section(f"KB2.e — Cross-source keyword coverage (last {days}d)")
    t0 = time.time()
    # Query từ keyword_events (raw) vì daily_keyword_stats không có cột source
    rows = ch.execute(f"""
        SELECT
            keyword,
            uniqExact(source) AS source_count,
            count() AS total_mentions
        FROM news.keyword_events
        WHERE publish_date >= today() - {days}
        GROUP BY keyword
        HAVING source_count >= 3
        ORDER BY source_count DESC, total_mentions DESC
        LIMIT 10
    """)
    elapsed_ms = (time.time() - t0) * 1000
    print(f"  Query time: {elapsed_ms:.1f} ms")
    print(f"  {'keyword':<40} {'sources':>8} {'mentions':>10}")
    for kw, sc, tm in rows:
        print(f"    {kw[:38]:<38} {sc:>8} {tm:>10}")


# =================================================================
# KỊCH BẢN 3: RBAC — so sánh index gốc vs alias
# =================================================================
def demo_kb3_rbac(es: Elasticsearch):
    print_section("KB3 — RBAC: admin (news_articles) vs guest (news_public)")
    admin_count = es.count(index="news_articles")["count"]
    guest_count = es.count(index="news_public")["count"]
    print(f"  Admin sees  : {admin_count:,} docs")
    print(f"  Guest sees  : {guest_count:,} docs (canonical only)")
    print(f"  Hidden      : {admin_count - guest_count:,} duplicate versions")

    # Demo: query 1 bài cụ thể — admin thấy author, guest source_excludes
    print()
    print("  Demo source filtering — Admin truy vấn với _source tự chọn, Guest bị ẩn author field:")
    try:
        # Get 1 doc có author để demo
        res = es.search(
            index="news_articles",
            query={"exists": {"field": "author"}},
            size=1,
            _source=True,
        )
        if res["hits"]["hits"]:
            doc = res["hits"]["hits"][0]
            aid = doc["_id"]
            admin_doc = doc["_source"]
            author_admin = admin_doc.get("author", "")

            # Guest query qua alias với _source_excludes
            res_guest = es.search(
                index="news_public",
                query={"term": {"_id": aid}},
                size=1,
                source_excludes=["author"],
            )
            guest_doc = res_guest["hits"]["hits"][0]["_source"] if res_guest["hits"]["hits"] else {}
            print(f"    Sample doc id: {aid[:12]}...")
            print(f"    Admin sees author: {author_admin!r}")
            print(f"    Guest sees author: {guest_doc.get('author', '(field not returned)')!r}")
    except Exception as e:
        print(f"  (demo skipped: {e})")


# =================================================================
# Main
# =================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--es", default="http://localhost:9200")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=9000)
    ap.add_argument("--search-query", default="Iran",
                    help="Từ khoá cho demo full-text search")
    ap.add_argument("--entity-query", default="Iran",
                    help="Entity text cho demo entity search")
    args = ap.parse_args()

    es = Elasticsearch(args.es, request_timeout=30)
    ch = CHClient(host=args.ch_host, port=args.ch_port, user="default", password="")

    print_header("INFRASTRUCTURE VERIFICATION")
    ok_es = check_es(es)
    ok_ch = check_ch(ch)
    if not (ok_es and ok_ch):
        print("\n✗ Some services unavailable, skipping demo queries")
        sys.exit(1)

    print_header("KỊCH BẢN 1 — SEARCH (Elasticsearch)")
    demo_kb1_fulltext_search(es, args.search_query)
    demo_kb1_entity_search(es, args.entity_query, "LOC")
    demo_kb1_combined(es, "tấn công", args.entity_query)

    print_header("KỊCH BẢN 2 — TRENDING (ClickHouse)")
    demo_kb2_top_keywords(ch, days=30)
    demo_kb2_top_keywords(ch, days=7, category="world")
    demo_kb2_hot_keywords(ch, window_days=7)
    demo_kb2_hourly_distribution(ch)
    demo_kb2_top_entities(ch, "LOC", days=30)
    demo_kb2_top_entities(ch, "PER", days=30)
    demo_kb2_cross_source_coverage(ch, days=30)

    print_header("KỊCH BẢN 3 — RBAC (Alias filtering)")
    demo_kb3_rbac(es)

    print("\n" + "=" * 70)
    print("✓ ALL DEMOS COMPLETED")
    print("=" * 70)


if __name__ == "__main__":
    main()
