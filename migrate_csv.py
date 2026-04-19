#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_csv.py
--------------
Migrate dữ liệu CSV từ crawler CŨ → JSONL format MỚI.

Xử lý các vấn đề của dataset cũ:
1. Fix source.name bị nhầm author (RSS_Feed crawler cũ)
   → source lấy từ domain URL, author lấy từ source.name cũ (nếu là tên người)
2. Parse keywords "a|b|c" → list
3. Clean content (strip boilerplate "Lưu bài viết thành công...")
4. Chuẩn hóa category
5. Generate các field mới: source_domain, source_type, content_length, has_full_content

Cách chạy:
    python migrate_csv.py \
        --csv 24h_html_categories_vi.csv:html \
        --csv znews_html_categories_vi.csv:html \
        --csv rss_feed_articles_v2.csv:rss \
        --output data/migrated_articles.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from typing import Dict, List, Optional

# Tăng field limit vì content có thể dài
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

from crawler_core import (
    Article,
    JsonlStore,
    stable_id,
    normalize_url,
    get_domain,
    domain_to_source,
    to_iso_utc,
    utc_now_iso,
    clean_content,
    normalize_category,
    extract_category_from_url,
    is_article_valid,
    MIN_CONTENT_LENGTH_STRICT,
    MIN_CONTENT_LENGTH_LOOSE,
    setup_logger,
)


log = setup_logger("migrate_csv")


# Heuristic: phát hiện `source.name` là tên người hay tên báo
# Các giá trị sau đây LÀ tên báo, không phải tên người
SOURCE_NAMES_ARE_BRANDS = {
    "RSS_Feed", "24h", "ZNews", "LaoDong", "VnExpress",
    "DanTri", "TuoiTre", "ThanhNien", "VietnamNet",
    "NLD", "VietnamPlus", "Soha", "NhanDan", "BaoTinTuc", "KienThuc",
}


def _is_likely_person_name(s: str) -> bool:
    """Heuristic: chuỗi có phải tên người tiếng Việt không."""
    if not s:
        return False
    s = s.strip()
    if s in SOURCE_NAMES_ARE_BRANDS:
        return False
    # Tên người thường: 2-5 từ, mỗi từ viết hoa đầu, không chứa số/dấu đặc biệt
    words = s.split()
    if not (2 <= len(words) <= 5):
        return False
    for w in words:
        if not w or not w[0].isupper():
            return False
        if any(ch.isdigit() or ch in "/@#_" for ch in w):
            return False
    return True


def migrate_row(row: Dict[str, str], default_source_type: str) -> Optional[Article]:
    """Migrate 1 row CSV thành Article object. Trả None nếu row không hợp lệ."""
    url = normalize_url(row.get("url", "").strip())
    if not url:
        return None

    title = (row.get("title") or "").strip()
    if not title:
        return None

    pub = to_iso_utc(row.get("published_at", "")) or ""
    if not pub:
        return None

    # Content: clean boilerplate
    content_raw = row.get("content.text") or row.get("content_text") or ""
    content = clean_content(content_raw)

    # Validation: bài RSS có thể ngắn (summary), HTML phải đủ dài (full content)
    min_len = MIN_CONTENT_LENGTH_LOOSE if default_source_type == "rss" else MIN_CONTENT_LENGTH_STRICT
    if not content or len(content) < min_len:
        return None

    # Source & author handling
    #   - Nếu source.name là tên người → đó là author, source = domain
    #   - Nếu source.name là tên báo → giữ, source = domain
    raw_source_name = (row.get("source.name") or "").strip()
    domain = get_domain(url)
    source = domain_to_source(domain)
    author = ""
    if _is_likely_person_name(raw_source_name):
        author = raw_source_name

    # Keywords: parse từ dạng "a|b|c" hoặc "a,b,c"
    kw_raw = row.get("keywords", "") or ""
    keywords: List[str] = []
    if kw_raw:
        if "|" in kw_raw:
            keywords = [k.strip() for k in kw_raw.split("|") if k.strip()]
        else:
            keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]

    # Category: có thể là "trang-chu" (rác) → fallback extract từ URL
    cat_raw = (row.get("category.primary") or "").strip()
    if not cat_raw or cat_raw == "trang-chu":
        cat_from_url = extract_category_from_url(url)
        if cat_from_url and cat_from_url != "trang-chu":
            cat_raw = cat_from_url

    return Article(
        id=stable_id(url),
        url=url,
        title=title,
        content=content,
        published_at=pub,
        crawled_at=utc_now_iso(),
        source=source,
        source_domain=domain,
        source_type=default_source_type,
        category_raw=cat_raw,
        category_normalized=normalize_category(cat_raw),
        author=author,
        language=(row.get("language") or "vi").strip().lower() or "vi",
        keywords=keywords,
        entities=[],
        content_length=len(content),
        has_full_content=len(content) >= 500,
    )


def migrate_file(csv_path: str, source_type: str, store: JsonlStore) -> Dict[str, int]:
    """Migrate 1 file CSV → store. Trả stats."""
    stats = {"total": 0, "added": 0, "skipped_dup": 0, "skipped_invalid": 0}
    log.info(f"Migrating {csv_path} (type={source_type})")

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total"] += 1
            article = migrate_row(row, default_source_type=source_type)
            if article is None:
                stats["skipped_invalid"] += 1
                continue
            if store.append(article):
                stats["added"] += 1
            else:
                stats["skipped_dup"] += 1

            if stats["total"] % 5000 == 0:
                log.info(f"  progress: {stats}")

    log.info(f"Done {csv_path}: {stats}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Migrate CSV → JSONL")
    parser.add_argument(
        "--csv", action="append", required=True,
        help="Format: path:source_type. Ví dụ: 24h.csv:html hoặc rss.csv:rss. Cho phép nhiều lần."
    )
    parser.add_argument("--output", type=str, default="data/migrated_articles.jsonl")
    args = parser.parse_args()

    store = JsonlStore(args.output)
    grand = {"total": 0, "added": 0, "skipped_dup": 0, "skipped_invalid": 0}

    for spec in args.csv:
        if ":" not in spec:
            log.error(f"Invalid --csv spec: {spec} (cần format path:type)")
            continue
        path, stype = spec.rsplit(":", 1)
        if not os.path.exists(path):
            log.error(f"File not found: {path}")
            continue
        if stype not in {"rss", "html"}:
            log.error(f"Invalid source_type: {stype} (chỉ chấp nhận 'rss' hoặc 'html')")
            continue
        stats = migrate_file(path, stype, store)
        for k, v in stats.items():
            grand[k] += v

    log.info("=" * 60)
    log.info(f"GRAND TOTAL: {grand}")
    log.info(f"Final count in store: {store.count():,}")
    log.info(f"Output: {args.output}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
