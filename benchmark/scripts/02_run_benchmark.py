#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
02_run_benchmark.py
-------------------
Chạy benchmark: so sánh latency giữa Elasticsearch, ClickHouse, PostgreSQL
cho cùng 1 loại query (apple-to-apple).

Methodology:
- Mỗi query chạy N lần (default 20). Bỏ warmup 3 runs đầu (cache cold).
- Report: min, median, p95, mean, stdev, throughput.
- Cùng dataset, cùng filter → kết quả chênh lệch là do engine.

Test categories:
1. FULL-TEXT SEARCH
   - ES: match query trên tsvector analyzer
   - PG tsvector: @@ to_tsquery (GIN index)
   - PG trigram: ILIKE '%x%' (GIN trigram index)
   - PG LIKE: naive LIKE (no index / seq scan) — baseline chậm

2. AGGREGATION / TRENDING
   - ClickHouse: GROUP BY trên SummingMergeTree MV
   - ClickHouse raw: GROUP BY trên keyword_events
   - PG: GROUP BY trên keyword_events

3. ENTITY SEARCH (nested)
   - ES: nested query
   - PG: JSONB @> containment (GIN index)

Output:
- results/benchmark_<timestamp>.json  : raw numbers
- results/benchmark_<timestamp>.md    : summary table cho báo cáo
- results/benchmark_<timestamp>.csv   : cho chart/plot ngoài

Usage:
    python 02_run_benchmark.py --runs 20
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

try:
    from elasticsearch import Elasticsearch
    from clickhouse_driver import Client as CHClient
    import psycopg2
except ImportError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    print("Run: pip install elasticsearch clickhouse-driver psycopg2-binary", file=sys.stderr)
    sys.exit(1)


# ==================================================================
# Benchmark harness
# ==================================================================
@dataclass
class BenchmarkResult:
    category: str           # "fulltext" | "aggregation" | "entity"
    scenario: str           # human-readable description
    engine: str             # "ES" | "ClickHouse" | "Postgres-tsvector" | ...
    query_name: str
    runs: int
    warmup: int
    latencies_ms: List[float] = field(default_factory=list)
    result_count: int = 0
    error: Optional[str] = None

    @property
    def measured(self) -> List[float]:
        return self.latencies_ms[self.warmup:]

    def summary(self) -> Dict[str, Any]:
        m = self.measured
        base = {
            "category": self.category,
            "scenario": self.scenario,
            "engine": self.engine,
            "query": self.query_name,
            "runs": len(m),
            "result_count": self.result_count,
        }
        if self.error:
            base["error"] = self.error
            return base
        if not m:
            return base
        base.update({
            "min_ms": round(min(m), 2),
            "median_ms": round(statistics.median(m), 2),
            "mean_ms": round(statistics.mean(m), 2),
            "p95_ms": round(sorted(m)[int(0.95 * len(m)) - 1] if len(m) >= 20 else max(m), 2),
            "max_ms": round(max(m), 2),
            "stdev_ms": round(statistics.stdev(m), 2) if len(m) > 1 else 0.0,
            "qps": round(1000.0 / statistics.mean(m), 1) if statistics.mean(m) > 0 else 0,
        })
        return base


def time_query(fn: Callable, runs: int, warmup: int) -> BenchmarkResult:
    """Chạy fn() `runs` lần (bao gồm warmup). Return object chứa latencies."""
    latencies = []
    count = 0
    error = None
    for i in range(runs):
        try:
            t0 = time.perf_counter()
            result = fn()
            elapsed = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed)
            if i == 0:  # grab count từ lần đầu
                count = result
        except Exception as e:
            error = str(e)[:200]
            break
    return BenchmarkResult(
        category="", scenario="", engine="", query_name="",
        runs=runs, warmup=warmup, latencies_ms=latencies,
        result_count=count, error=error,
    )


# ==================================================================
# Query callables — mỗi engine có cách đếm result riêng
# ==================================================================
def mk_es_fulltext(es: Elasticsearch, index: str, q: str):
    """ES full-text. AND semantics để khớp PG plainto_tsquery (tránh apples-vs-oranges)."""
    def run():
        res = es.search(
            index=index,
            body={
                "query": {
                    "multi_match": {
                        "query": q,
                        "fields": ["title^3", "content"],
                        "type": "best_fields",
                        # Align với PG plainto_tsquery (default AND).
                        # Không có operator này, ES dùng OR mặc định → count gấp 3-6× PG → benchmark vô nghĩa.
                        "operator": "and",
                    }
                },
                "size": 10,
                "track_total_hits": True,
            },
        )
        return res["hits"]["total"]["value"]
    return run


def mk_pg_fts(conn, q: str):
    def run():
        cur = conn.cursor()
        # Normalize: remove Vietnamese tones để match unaccent
        cur.execute("""
            SELECT count(*) FROM articles
            WHERE fts @@ plainto_tsquery('simple', unaccent(%s))
        """, (q,))
        c = cur.fetchone()[0]
        cur.close()
        return c
    return run


def mk_pg_trigram(conn, q: str):
    def run():
        cur = conn.cursor()
        like_q = f"%{q}%"
        cur.execute("""
            SELECT count(*) FROM articles
            WHERE title ILIKE %s OR content ILIKE %s
        """, (like_q, like_q))
        c = cur.fetchone()[0]
        cur.close()
        return c
    return run


def mk_pg_like_noindex(conn, q: str):
    """Baseline: force disable GIN trigram index để thấy LIKE thuần."""
    def run():
        cur = conn.cursor()
        like_q = f"%{q}%"
        # set_local chỉ áp dụng trong transaction; ta dùng SET ... LOCAL
        cur.execute("BEGIN")
        cur.execute("SET LOCAL enable_bitmapscan = off")
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute("""
            SELECT count(*) FROM articles
            WHERE title LIKE %s OR content LIKE %s
        """, (like_q, like_q))
        c = cur.fetchone()[0]
        cur.execute("COMMIT")
        cur.close()
        return c
    return run


def mk_es_entity(es: Elasticsearch, index: str, entity_text: str, entity_type: str):
    def run():
        res = es.search(
            index=index,
            body={
                "query": {
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
                "size": 10,
                "track_total_hits": True,
            },
        )
        return res["hits"]["total"]["value"]
    return run


def mk_pg_entity(conn, entity_text: str, entity_type: str):
    def run():
        cur = conn.cursor()
        # JSONB containment: entities @> '[{"text": "x", "type": "y"}]'
        contain = json.dumps([{"text": entity_text, "type": entity_type}])
        cur.execute("""
            SELECT count(*) FROM articles
            WHERE entities @> %s::jsonb
        """, (contain,))
        c = cur.fetchone()[0]
        cur.close()
        return c
    return run


def mk_ch_top_keywords(ch: CHClient, days: int, use_mv: bool):
    """Top keywords trong N ngày."""
    if use_mv:
        sql = """
            SELECT keyword, sum(mention_count) AS c
            FROM news.daily_keyword_stats
            WHERE publish_date >= today() - %(d)s
            GROUP BY keyword ORDER BY c DESC LIMIT 20
        """
    else:
        sql = """
            SELECT keyword, count() AS c
            FROM news.keyword_events
            WHERE publish_date >= today() - %(d)s
            GROUP BY keyword ORDER BY c DESC LIMIT 20
        """
    def run():
        rows = ch.execute(sql, {"d": days})
        return len(rows)
    return run


def mk_pg_top_keywords(conn, days: int):
    def run():
        cur = conn.cursor()
        cur.execute("""
            SELECT keyword, count(*) AS c
            FROM keyword_events
            WHERE publish_date >= CURRENT_DATE - %s::int
            GROUP BY keyword ORDER BY c DESC LIMIT 20
        """, (days,))
        rows = cur.fetchall()
        cur.close()
        return len(rows)
    return run


def mk_ch_hot_spike(ch: CHClient, window: int):
    """Hot spike detection query — phức tạp nhất."""
    sql = """
        WITH
            cur AS (
                SELECT keyword, sum(mention_count) AS cnt_now
                FROM news.daily_keyword_stats
                WHERE publish_date >= today() - %(w)s
                GROUP BY keyword
            ),
            prev AS (
                SELECT keyword, sum(mention_count) AS cnt_prev
                FROM news.daily_keyword_stats
                WHERE publish_date >= today() - %(w2)s AND publish_date < today() - %(w)s
                GROUP BY keyword
            )
        SELECT c.keyword, c.cnt_now, coalesce(p.cnt_prev, 0),
               round(c.cnt_now / greatest(p.cnt_prev, 1), 2) AS mult
        FROM cur c LEFT JOIN prev p ON c.keyword = p.keyword
        WHERE c.cnt_now >= 5
        ORDER BY mult DESC, c.cnt_now DESC LIMIT 20
    """
    def run():
        rows = ch.execute(sql, {"w": window, "w2": window * 2})
        return len(rows)
    return run


def mk_pg_hot_spike(conn, window: int):
    def run():
        cur = conn.cursor()
        cur.execute("""
            WITH cur AS (
                SELECT keyword, count(*) AS cnt_now
                FROM keyword_events
                WHERE publish_date >= CURRENT_DATE - %s::int
                GROUP BY keyword
            ),
            prev AS (
                SELECT keyword, count(*) AS cnt_prev
                FROM keyword_events
                WHERE publish_date >= CURRENT_DATE - (%s::int * 2)
                  AND publish_date < CURRENT_DATE - %s::int
                GROUP BY keyword
            )
            SELECT c.keyword, c.cnt_now, coalesce(p.cnt_prev, 0),
                   round(c.cnt_now::numeric / greatest(p.cnt_prev, 1), 2) AS mult
            FROM cur c LEFT JOIN prev p ON c.keyword = p.keyword
            WHERE c.cnt_now >= 5
            ORDER BY mult DESC, c.cnt_now DESC LIMIT 20
        """, (window, window, window))
        rows = cur.fetchall()
        cur.close()
        return len(rows)
    return run


def mk_ch_cross_source(ch: CHClient, days: int, min_sources: int):
    # MV daily_keyword_stats không có cột `source` (bị aggregate ra). Phải query
    # từ raw table news.keyword_events — fair với Postgres cũng query raw events.
    sql = """
        SELECT keyword, uniqExact(source) AS sc, count() AS tm
        FROM news.keyword_events
        WHERE publish_date >= today() - %(d)s
        GROUP BY keyword HAVING sc >= %(ms)s
        ORDER BY sc DESC LIMIT 20
    """
    def run():
        rows = ch.execute(sql, {"d": days, "ms": min_sources})
        return len(rows)
    return run


def mk_pg_cross_source(conn, days: int, min_sources: int):
    def run():
        cur = conn.cursor()
        cur.execute("""
            SELECT keyword, count(distinct source) AS sc, count(*) AS tm
            FROM keyword_events
            WHERE publish_date >= CURRENT_DATE - %s::int
            GROUP BY keyword HAVING count(distinct source) >= %s::int
            ORDER BY sc DESC LIMIT 20
        """, (days, min_sources))
        rows = cur.fetchall()
        cur.close()
        return len(rows)
    return run


# ==================================================================
# Benchmark plan
# ==================================================================
def build_benchmark_plan(es, ch, pg, es_index="news_articles") -> List[Dict[str, Any]]:
    """Return list of benchmarks to run. Each item: {category, scenario, engine, query_name, fn}"""
    # Pick các query từ thực tế trên dataset
    search_terms = ["Iran", "giá vàng", "Việt Nam", "bóng đá"]
    entities_to_test = [("Iran", "LOC"), ("Trump", "PER"), ("Việt Nam", "LOC"), ("Real Madrid", "ORG")]

    plan = []

    # ===== 1. FULL-TEXT SEARCH =====
    for term in search_terms:
        plan.extend([
            {"category": "fulltext", "scenario": f"Search '{term}'", "engine": "Elasticsearch",
             "query_name": "multi_match", "fn": mk_es_fulltext(es, es_index, term)},
            {"category": "fulltext", "scenario": f"Search '{term}'", "engine": "PG-tsvector",
             "query_name": "fts @@ tsquery", "fn": mk_pg_fts(pg, term)},
            {"category": "fulltext", "scenario": f"Search '{term}'", "engine": "PG-trigram",
             "query_name": "ILIKE (GIN trigram)", "fn": mk_pg_trigram(pg, term)},
            {"category": "fulltext", "scenario": f"Search '{term}'", "engine": "PG-seqscan",
             "query_name": "LIKE no index", "fn": mk_pg_like_noindex(pg, term)},
        ])

    # ===== 2. AGGREGATION / TRENDING =====
    for days in [7, 30, 90]:
        plan.extend([
            {"category": "aggregation", "scenario": f"Top keywords {days}d", "engine": "ClickHouse-MV",
             "query_name": "daily_keyword_stats GROUP BY", "fn": mk_ch_top_keywords(ch, days, True)},
            {"category": "aggregation", "scenario": f"Top keywords {days}d", "engine": "ClickHouse-raw",
             "query_name": "keyword_events GROUP BY", "fn": mk_ch_top_keywords(ch, days, False)},
            {"category": "aggregation", "scenario": f"Top keywords {days}d", "engine": "Postgres",
             "query_name": "keyword_events GROUP BY", "fn": mk_pg_top_keywords(pg, days)},
        ])

    # Complex aggregation: hot spike
    plan.extend([
        {"category": "aggregation", "scenario": "Hot spike detection 7d", "engine": "ClickHouse-MV",
         "query_name": "2 CTE + JOIN", "fn": mk_ch_hot_spike(ch, 7)},
        {"category": "aggregation", "scenario": "Hot spike detection 7d", "engine": "Postgres",
         "query_name": "2 CTE + JOIN", "fn": mk_pg_hot_spike(pg, 7)},
    ])

    plan.extend([
        {"category": "aggregation", "scenario": "Cross-source 30d (>=3 sources)", "engine": "ClickHouse-raw",
         "query_name": "uniqExact + HAVING (keyword_events)", "fn": mk_ch_cross_source(ch, 30, 3)},
        {"category": "aggregation", "scenario": "Cross-source 30d (>=3 sources)", "engine": "Postgres",
         "query_name": "count distinct + HAVING", "fn": mk_pg_cross_source(pg, 30, 3)},
    ])

    # ===== 3. ENTITY SEARCH =====
    for text, etype in entities_to_test:
        plan.extend([
            {"category": "entity", "scenario": f"{etype}='{text}'", "engine": "Elasticsearch",
             "query_name": "nested query", "fn": mk_es_entity(es, es_index, text, etype)},
            {"category": "entity", "scenario": f"{etype}='{text}'", "engine": "Postgres",
             "query_name": "JSONB @> GIN", "fn": mk_pg_entity(pg, text, etype)},
        ])

    return plan


# ==================================================================
# Runner
# ==================================================================
def run_all_benchmarks(plan, runs, warmup) -> List[BenchmarkResult]:
    results = []
    for i, item in enumerate(plan, 1):
        print(f"  [{i}/{len(plan)}] {item['category']:<12} | {item['engine']:<18} | {item['scenario']}", end=" ... ", flush=True)
        r = time_query(item["fn"], runs=runs, warmup=warmup)
        r.category = item["category"]
        r.scenario = item["scenario"]
        r.engine = item["engine"]
        r.query_name = item["query_name"]
        if r.error:
            print(f"ERROR: {r.error[:50]}")
        else:
            s = r.summary()
            print(f"median={s['median_ms']}ms  count={s['result_count']:,}")
        results.append(r)
    return results


def save_results(results: List[BenchmarkResult], outdir: str):
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summaries = [r.summary() for r in results]

    # JSON (ép utf-8 vì Windows default cp1252 sẽ fail trên ký tự tiếng Việt)
    json_path = os.path.join(outdir, f"benchmark_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False)

    # CSV
    csv_path = os.path.join(outdir, f"benchmark_{ts}.csv")
    all_keys = set()
    for s in summaries:
        all_keys.update(s.keys())
    cols = ["category", "scenario", "engine", "query", "runs", "result_count",
            "min_ms", "median_ms", "mean_ms", "p95_ms", "max_ms", "stdev_ms", "qps", "error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in summaries:
            w.writerow(s)

    # Markdown report
    md_path = os.path.join(outdir, f"benchmark_{ts}.md")
    write_markdown_report(md_path, summaries)

    print()
    print(f"✓ Saved: {json_path}")
    print(f"✓ Saved: {csv_path}")
    print(f"✓ Saved: {md_path}")


def write_markdown_report(path: str, summaries: List[Dict[str, Any]]):
    """Markdown report có bảng + speedup calculation."""
    # Group by (category, scenario)
    from collections import defaultdict
    groups = defaultdict(list)
    for s in summaries:
        groups[(s["category"], s["scenario"])].append(s)

    lines = []
    lines.append("# Benchmark Report")
    lines.append(f"\nGenerated at: `{datetime.now().isoformat()}`")
    lines.append("\nMethodology: mỗi query chạy 20 lần, bỏ 3 warmup run. Latency report median + p95.")
    lines.append("")

    # Group by category
    cats = defaultdict(list)
    for (cat, scen), rows in groups.items():
        cats[cat].append((scen, rows))

    for cat, items in cats.items():
        lines.append(f"\n## {cat.upper()}\n")
        for scen, rows in items:
            lines.append(f"\n### {scen}\n")
            lines.append("| Engine | Query | Median | P95 | Max | Stdev | QPS | Count |")
            lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
            # Sort theo median (nhanh nhất lên trên)
            rows_sorted = sorted([r for r in rows if "median_ms" in r], key=lambda r: r["median_ms"])
            rows_err = [r for r in rows if "error" in r]
            fastest = rows_sorted[0]["median_ms"] if rows_sorted else None
            for r in rows_sorted:
                speedup = ""
                if fastest and r["median_ms"] > fastest:
                    speedup = f" ({r['median_ms']/fastest:.1f}× slower)"
                elif fastest and r["median_ms"] == fastest:
                    speedup = " **(fastest)**"
                lines.append(
                    f"| {r['engine']} | {r['query']} | {r['median_ms']}ms{speedup} | "
                    f"{r['p95_ms']}ms | {r['max_ms']}ms | {r['stdev_ms']}ms | "
                    f"{r['qps']} | {r['result_count']:,} |"
                )
            for r in rows_err:
                lines.append(f"| {r['engine']} | {r['query']} | ERROR: {r['error'][:60]} | - | - | - | - | - |")

    # Overall verdict
    lines.append("\n## Summary")
    lines.append("")
    lines.append("Engine ranking theo median latency trung bình:")
    engine_medians = defaultdict(list)
    for s in summaries:
        if "median_ms" in s:
            engine_medians[s["engine"]].append(s["median_ms"])
    ranking = sorted(
        [(e, statistics.median(ms)) for e, ms in engine_medians.items()],
        key=lambda x: x[1],
    )
    for rank, (engine, m) in enumerate(ranking, 1):
        lines.append(f"{rank}. **{engine}**: median {m:.1f}ms overall")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ==================================================================
# Main
# ==================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--es",      default="http://localhost:9200")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=9000)
    ap.add_argument("--pg-host", default="localhost")
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument("--pg-db",   default="news_bench")
    ap.add_argument("--pg-user", default="bench")
    ap.add_argument("--pg-pw",   default="bench")
    ap.add_argument("--runs",    type=int, default=20, help="Runs per query (including warmup)")
    ap.add_argument("--warmup",  type=int, default=3)
    ap.add_argument("--outdir",  default="results")
    ap.add_argument("--es-index", default="news_articles")
    ap.add_argument("--skip-fulltext", action="store_true")
    ap.add_argument("--skip-aggregation", action="store_true")
    ap.add_argument("--skip-entity", action="store_true")
    args = ap.parse_args()

    # Connect
    print("Connecting to services...")
    es = Elasticsearch(args.es, request_timeout=60)
    assert es.ping(), f"ES not reachable at {args.es}"
    ch = CHClient(host=args.ch_host, port=args.ch_port, user="default", password="")
    ch.execute("SELECT 1")
    pg = psycopg2.connect(
        host=args.pg_host, port=args.pg_port, dbname=args.pg_db,
        user=args.pg_user, password=args.pg_pw,
    )
    pg.set_session(autocommit=True)
    print("✓ All 3 engines reachable")
    print()

    # Print dataset sizes
    es_count = es.count(index=args.es_index)["count"]
    ch_articles = ch.execute("SELECT count() FROM news.articles")[0][0]
    ch_kw = ch.execute("SELECT count() FROM news.keyword_events")[0][0]
    cur = pg.cursor()
    cur.execute("SELECT count(*) FROM articles"); pg_articles = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM keyword_events"); pg_kw = cur.fetchone()[0]
    cur.close()
    print(f"Dataset sizes:")
    print(f"  ES articles         : {es_count:,}")
    print(f"  CH articles         : {ch_articles:,}")
    print(f"  CH keyword_events   : {ch_kw:,}")
    print(f"  PG articles         : {pg_articles:,}")
    print(f"  PG keyword_events   : {pg_kw:,}")
    print()

    # Build plan + filter
    plan = build_benchmark_plan(es, ch, pg, es_index=args.es_index)
    if args.skip_fulltext:
        plan = [p for p in plan if p["category"] != "fulltext"]
    if args.skip_aggregation:
        plan = [p for p in plan if p["category"] != "aggregation"]
    if args.skip_entity:
        plan = [p for p in plan if p["category"] != "entity"]

    print(f"Running {len(plan)} benchmarks, each {args.runs} times ({args.warmup} warmup)...")
    print()
    results = run_all_benchmarks(plan, runs=args.runs, warmup=args.warmup)

    # Save
    save_results(results, args.outdir)


if __name__ == "__main__":
    main()
