#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_storage_compare.py
---------------------
Đo dung lượng disk khi lưu cùng dataset vào 4 engine:
  - PostgreSQL  (row-store + B-tree/GIN)
  - MongoDB     (BSON + WiredTiger snappy)
  - Elasticsearch (Lucene inverted index, doc_values, stored fields)
  - ClickHouse  (columnar + LZ4 compression default)

Baseline: tổng size của file ../data/articles_ready*.jsonl gốc.

Metrics report:
  - data_size     — kích thước data thô (uncompressed nếu engine compress)
  - index_size    — tổng size của index/secondary structures
  - disk_size     — disk usage THỰC TẾ (sau compression nếu có)
  - vs_raw_ratio  — disk_size / raw_jsonl_size (càng nhỏ = càng tiết kiệm)

Output:
  - results/storage_<ts>.json  raw numbers
  - results/storage_<ts>.csv   bảng cho Excel
  - results/storage_<ts>.md    bảng + breakdown cho báo cáo

Usage:
    python 04_storage_compare.py
    python 04_storage_compare.py --skip-mongo --skip-es
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


# ==================================================================
# Helpers
# ==================================================================
def fmt_bytes(n: int) -> str:
    """1234567 → '1.18 MB'"""
    if n is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if abs(f) < 1024.0:
            return f"{f:.2f} {u}"
        f /= 1024.0
    return f"{f:.2f} PB"


def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        print(f"    (skip: {type(e).__name__}: {str(e)[:100]})", file=sys.stderr)
        return default


# ==================================================================
# Per-engine size collectors
# ==================================================================
def measure_raw_jsonl(patterns: List[str]) -> Dict[str, Any]:
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    files.sort()
    total = sum(os.path.getsize(f) for f in files)
    return {
        "engine": "Raw JSONL",
        "entries": [
            {
                "table": "articles_ready*.jsonl",
                "row_count": None,  # đếm rows tốn IO, skip — đã có ở PG/Mongo
                "data_size": total,
                "index_size": 0,
                "disk_size": total,
            }
        ],
        "files": files,
    }


def measure_pg(host, port, db, user, pw) -> Dict[str, Any]:
    import psycopg2
    conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pw)
    conn.set_session(autocommit=True)
    cur = conn.cursor()
    # pg_relation_size = main table only (heap, không TOAST không index)
    # pg_table_size    = heap + TOAST + FSM/VM
    # pg_indexes_size  = sum of all indexes on table
    # pg_total_relation_size = pg_table_size + pg_indexes_size
    cur.execute("""
        SELECT
            c.relname AS table,
            pg_table_size(c.oid)         AS data_size,
            pg_indexes_size(c.oid)       AS index_size,
            pg_total_relation_size(c.oid) AS disk_size,
            coalesce(s.n_live_tup, 0)    AS row_count
        FROM pg_class c
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relkind = 'r'
          AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
          AND c.relname IN ('articles', 'keyword_events', 'entity_events')
        ORDER BY c.relname
    """)
    entries = []
    for table, data, idx, total, rows in cur.fetchall():
        entries.append({
            "table": table,
            "row_count": int(rows),
            "data_size": int(data),
            "index_size": int(idx),
            "disk_size": int(total),
        })
    cur.close()
    conn.close()
    return {"engine": "PostgreSQL", "entries": entries}


def measure_mongo(host, port, db_name, user, pw, auth_db) -> Dict[str, Any]:
    from pymongo import MongoClient
    client = MongoClient(
        host=host, port=port,
        username=user, password=pw,
        authSource=auth_db,
        serverSelectionTimeoutMS=10000,
    )
    client.server_info()
    db = client[db_name]
    entries = []
    for coll_name in ("articles", "keyword_events", "entity_events"):
        if coll_name not in db.list_collection_names():
            continue
        # collStats: size (uncompressed BSON), storageSize (on disk),
        # totalIndexSize, count
        st = db.command("collStats", coll_name)
        entries.append({
            "table": coll_name,
            "row_count": int(st.get("count", 0)),
            # "data_size" = uncompressed BSON để fair so với PG pg_table_size
            "data_size": int(st.get("size", 0)),
            "index_size": int(st.get("totalIndexSize", 0)),
            # disk = storage (compressed) + index
            "disk_size": int(st.get("storageSize", 0)) + int(st.get("totalIndexSize", 0)),
            # Extra info riêng cho Mongo
            "_storage_compressed": int(st.get("storageSize", 0)),
        })
    client.close()
    return {"engine": "MongoDB", "entries": entries}


def measure_es(es_url) -> Dict[str, Any]:
    from elasticsearch import Elasticsearch
    es = Elasticsearch(es_url, request_timeout=30)
    if not es.ping():
        raise RuntimeError(f"ES not reachable at {es_url}")
    # _stats: store, segments, fielddata, etc.
    # Chỉ đo news_articles (ES không có analog cho keyword_events / entity_events)
    indices = ["news_articles"]
    stats = es.indices.stats(index=",".join(indices), level="indices")
    entries = []
    for idx_name, idx_data in stats["indices"].items():
        primaries = idx_data["primaries"]
        store = primaries["store"]["size_in_bytes"]
        segments = primaries["segments"]
        # Lucene không tách "data" vs "index" rõ rệt như SQL.
        # Approx: stored_fields = data (source + stored), còn lại là index.
        # Lấy từ segments.* nếu có (memory in heap, không phải disk):
        # ES không trực tiếp cho biết breakdown disk → ta dùng store toàn bộ
        # vào "disk_size", và để index_size = 0 với note.
        entries.append({
            "table": idx_name,
            "row_count": int(primaries["docs"]["count"]),
            # Lucene index = stored fields + inverted index + doc_values trộn
            # vào 1 segment. "data_size" và "index_size" không tách được clean.
            "data_size": int(store),
            "index_size": 0,    # bundled vào store
            "disk_size": int(store),
            "_note": "Lucene: data + inverted index bundled trong segments",
        })
    return {"engine": "Elasticsearch", "entries": entries}


def measure_ch(host, port) -> Dict[str, Any]:
    from clickhouse_driver import Client
    ch = Client(host=host, port=port, user="default", password="")
    ch.execute("SELECT 1")
    # system.parts: bytes_on_disk = compressed, data_uncompressed_bytes = raw
    rows = ch.execute("""
        SELECT
            table,
            sum(rows)                         AS row_count,
            sum(data_uncompressed_bytes)      AS data_size,
            sum(primary_key_bytes_in_memory)  AS index_size,
            sum(bytes_on_disk)                AS disk_size
        FROM system.parts
        WHERE database = 'news' AND active = 1
        GROUP BY table
        ORDER BY table
    """)
    entries = []
    for table, rc, data, idx, disk in rows:
        entries.append({
            "table": table,
            "row_count": int(rc),
            "data_size": int(data),
            # CH primary_key chỉ là sparse index ~vài KB → tiny, just record nó
            "index_size": int(idx),
            "disk_size": int(disk),
            "_compression_ratio": (float(data) / float(disk)) if disk > 0 else 0,
        })
    return {"engine": "ClickHouse", "entries": entries}


# ==================================================================
# Reporting
# ==================================================================
def write_markdown(path: str, snapshots: List[Dict[str, Any]], raw_bytes: int):
    lines = []
    lines.append("# Storage Size Comparison")
    lines.append("")
    lines.append(f"Generated: `{datetime.now().isoformat(timespec='seconds')}`")
    lines.append("")
    lines.append(
        "Baseline: tổng size của `../data/articles_ready*.jsonl` "
        f"= **{fmt_bytes(raw_bytes)}**. Cột `vs Raw` là tỉ lệ "
        "`disk_size / raw_jsonl` (< 1 = engine nén tốt, > 1 = phồng to vì index)."
    )
    lines.append("")

    # ===== Per-engine totals =====
    lines.append("## Tổng disk usage theo engine\n")
    lines.append("| Engine | Rows (articles) | Data (uncompressed) | Indexes | Disk total | vs Raw |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for snap in snapshots:
        eng = snap["engine"]
        total_data = sum(e["data_size"] for e in snap["entries"])
        total_idx = sum(e.get("index_size", 0) for e in snap["entries"])
        total_disk = sum(e["disk_size"] for e in snap["entries"])
        # Lấy row_count của articles (table chính) để hiển thị
        art_rows = next(
            (e["row_count"] for e in snap["entries"]
             if e["table"] in ("articles", "news_articles", "articles_ready*.jsonl")
             and e["row_count"] is not None),
            None,
        )
        rows_str = f"{art_rows:,}" if art_rows else "—"
        ratio = total_disk / raw_bytes if raw_bytes > 0 else 0
        lines.append(
            f"| {eng} | {rows_str} | {fmt_bytes(total_data)} | "
            f"{fmt_bytes(total_idx)} | **{fmt_bytes(total_disk)}** | {ratio:.2f}× |"
        )
    lines.append("")

    # ===== Breakdown per table/collection =====
    lines.append("## Breakdown theo bảng/collection\n")
    # Aggregate: table → engine → entry
    tables = []
    by_table: Dict[str, Dict[str, Dict]] = {}
    for snap in snapshots:
        for e in snap["entries"]:
            t = e["table"]
            if t not in by_table:
                by_table[t] = {}
                tables.append(t)
            by_table[t][snap["engine"]] = e

    for t in tables:
        lines.append(f"\n### `{t}`\n")
        lines.append("| Engine | Rows | Data | Index | Disk | Compression |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for eng_name in [s["engine"] for s in snapshots]:
            e = by_table[t].get(eng_name)
            if not e:
                continue
            data = e["data_size"]
            idx = e.get("index_size", 0)
            disk = e["disk_size"]
            rc = e.get("row_count")
            rows_str = f"{rc:,}" if rc is not None else "—"
            compr = (data / disk) if disk > 0 else 0
            compr_str = f"{compr:.2f}×" if data > 0 and disk > 0 else "—"
            lines.append(
                f"| {eng_name} | {rows_str} | {fmt_bytes(data)} | "
                f"{fmt_bytes(idx)} | {fmt_bytes(disk)} | {compr_str} |"
            )

    # ===== Notes =====
    lines.append("\n## Lưu ý fair-comparison\n")
    lines.append(
        "- **PostgreSQL**: `pg_table_size` (heap + TOAST) cho data; `pg_indexes_size` cho index. "
        "TOAST chứa cả content compressed (PGLZ) nên `data_size` đã hơi compressed.\n"
        "- **MongoDB**: `size` = uncompressed BSON, `storageSize` = sau WiredTiger snappy. "
        "Cột `Disk` = `storageSize + totalIndexSize`.\n"
        "- **Elasticsearch**: Lucene gộp stored fields + inverted index + doc_values "
        "vào segment → KHÔNG tách được clean `data` vs `index` → cả 2 cộng dồn vào `Disk`. "
        "ES không có collection mirror cho `keyword_events`/`entity_events`.\n"
        "- **ClickHouse**: `data_uncompressed_bytes` cho data, `bytes_on_disk` (LZ4 default) "
        "cho disk. Cột `Compression` ở CH thường rất cao (3-10×) — đây là điểm mạnh của columnar.\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_csv(path: str, snapshots: List[Dict[str, Any]]):
    cols = ["engine", "table", "row_count", "data_size", "index_size", "disk_size"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for snap in snapshots:
            for e in snap["entries"]:
                row = {"engine": snap["engine"]}
                row.update(e)
                w.writerow(row)


def write_json(path: str, snapshots, raw_bytes):
    out = {
        "generated_at": datetime.now().isoformat(),
        "raw_jsonl_bytes": raw_bytes,
        "snapshots": snapshots,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)


# ==================================================================
# Main
# ==================================================================
def main():
    ap = argparse.ArgumentParser()
    # Raw JSONL
    ap.add_argument("--input", action="append", default=None,
                    help="Raw JSONL pattern(s). Default: ../data/articles_ready*.jsonl")
    # Postgres
    ap.add_argument("--pg-host", default="localhost")
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument("--pg-db", default="news_bench")
    ap.add_argument("--pg-user", default="bench")
    ap.add_argument("--pg-pw", default="bench")
    # Mongo
    ap.add_argument("--mongo-host", default="localhost")
    ap.add_argument("--mongo-port", type=int, default=27017)
    ap.add_argument("--mongo-db", default="news_bench")
    ap.add_argument("--mongo-user", default="bench")
    ap.add_argument("--mongo-pw", default="bench")
    ap.add_argument("--mongo-auth-db", default="admin")
    # ES + CH
    ap.add_argument("--es", default="http://localhost:9200")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=9000)
    # Skips
    ap.add_argument("--skip-pg", action="store_true")
    ap.add_argument("--skip-mongo", action="store_true")
    ap.add_argument("--skip-es", action="store_true")
    ap.add_argument("--skip-ch", action="store_true")
    # Output
    ap.add_argument("--outdir", default="results")
    args = ap.parse_args()

    patterns = args.input or ["../data/articles_ready*.jsonl"]

    # Collect raw JSONL baseline
    print("Measuring raw JSONL baseline...")
    raw = measure_raw_jsonl(patterns)
    raw_bytes = sum(e["disk_size"] for e in raw["entries"])
    print(f"  ✓ {fmt_bytes(raw_bytes)} ({len(raw['files'])} files)")

    snapshots: List[Dict] = [raw]

    if not args.skip_pg:
        print("Measuring PostgreSQL...")
        snap = safe(lambda: measure_pg(
            args.pg_host, args.pg_port, args.pg_db, args.pg_user, args.pg_pw))
        if snap:
            for e in snap["entries"]:
                print(f"  ✓ {e['table']}: data={fmt_bytes(e['data_size'])}, "
                      f"idx={fmt_bytes(e['index_size'])}, disk={fmt_bytes(e['disk_size'])}")
            snapshots.append(snap)

    if not args.skip_mongo:
        print("Measuring MongoDB...")
        snap = safe(lambda: measure_mongo(
            args.mongo_host, args.mongo_port, args.mongo_db,
            args.mongo_user, args.mongo_pw, args.mongo_auth_db))
        if snap:
            for e in snap["entries"]:
                print(f"  ✓ {e['table']}: data(BSON)={fmt_bytes(e['data_size'])}, "
                      f"idx={fmt_bytes(e['index_size'])}, disk={fmt_bytes(e['disk_size'])}")
            snapshots.append(snap)

    if not args.skip_es:
        print("Measuring Elasticsearch...")
        snap = safe(lambda: measure_es(args.es))
        if snap:
            for e in snap["entries"]:
                print(f"  ✓ {e['table']}: store={fmt_bytes(e['disk_size'])}")
            snapshots.append(snap)

    if not args.skip_ch:
        print("Measuring ClickHouse...")
        snap = safe(lambda: measure_ch(args.ch_host, args.ch_port))
        if snap:
            for e in snap["entries"]:
                print(f"  ✓ {e['table']}: data={fmt_bytes(e['data_size'])}, "
                      f"disk={fmt_bytes(e['disk_size'])} (compr {e.get('_compression_ratio', 0):.2f}×)")
            snapshots.append(snap)

    # Save
    os.makedirs(args.outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(args.outdir, f"storage_{ts}.json")
    csv_path = os.path.join(args.outdir, f"storage_{ts}.csv")
    md_path = os.path.join(args.outdir, f"storage_{ts}.md")
    write_json(json_path, snapshots, raw_bytes)
    write_csv(csv_path, snapshots)
    write_markdown(md_path, snapshots, raw_bytes)
    print()
    print(f"✓ Saved: {json_path}")
    print(f"✓ Saved: {csv_path}")
    print(f"✓ Saved: {md_path}")


if __name__ == "__main__":
    main()
