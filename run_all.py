#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py
----------
Entry point chính: chạy cả RSS crawler và HTML crawler, rồi post-process
(cleanup + NER) để dataset sẵn sàng index.

Pipeline:
    1. RSS crawler       → data/rss_articles.jsonl
    2. HTML crawler      → data/html_articles.jsonl
    3. Build dataset     → data/articles_final*.jsonl
    4. Cleanup           → data/articles_cleaned*.jsonl  (FULL REBUILD, ~1-2 phút)
    5. NER               → data/articles_ner*.jsonl      (INCREMENTAL, chỉ NER bài mới)

Usage:
    python run_all.py                     # chạy tất cả với default
    python run_all.py --rss-only          # chỉ RSS
    python run_all.py --html-only         # chỉ HTML
    python run_all.py --start 2026-01-01  # từ ngày cụ thể
    python run_all.py --no-enrich         # RSS không enrich full content
    python run_all.py --skip-cleanup      # bỏ qua bước cleanup
    python run_all.py --skip-ner          # bỏ qua bước NER
    python run_all.py --ner-workers 4     # NER với 4 workers
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta

from rss_crawler import run_rss_crawler, RSS_FEEDS
from html_crawler import run_html_crawler, SITE_CONFIG
from build_dataset import build_final_dataset
from crawler_core import setup_logger


def default_start_date(days_back: int = 7) -> str:
    """Mặc định lấy bài từ N ngày trước (tối ưu cho daily run)."""
    return (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")


def main():
    parser = argparse.ArgumentParser(description="News Crawler Runner")
    parser.add_argument("--rss-only", action="store_true", help="Chỉ chạy RSS crawler")
    parser.add_argument("--html-only", action="store_true", help="Chỉ chạy HTML crawler")
    parser.add_argument("--start", type=str, default=default_start_date(),
                        help="Ngày bắt đầu YYYY-MM-DD (default: 7 ngày trước)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="RSS không enrich full content từ URL gốc (nhanh hơn)")
    parser.add_argument("--delay", type=float, default=0.8,
                        help="Min delay per domain (giây). Default 0.8")
    parser.add_argument("--max-pages", type=int, default=50,
                        help="Max pages per category cho HTML crawler. Default 50")
    parser.add_argument("--output-dir", type=str, default="data",
                        help="Thư mục output. Default: data/")
    parser.add_argument("--skip-build", action="store_true",
                        help="Bỏ qua bước build articles_final.jsonl sau khi crawl")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Bỏ qua bước cleanup (pre_dataset/01_cleanup.py)")
    parser.add_argument("--skip-ner", action="store_true",
                        help="Bỏ qua bước NER (pre_dataset/02_ner.py)")
    parser.add_argument("--ner-workers", type=int, default=2,
                        help="Số process song song cho NER. Default 2 (an toàn GHA). "
                             "Local khuyên --ner-workers 4")
    parser.add_argument("--ner-title-only", action="store_true",
                        help="NER chỉ chạy trên title (nhanh, dùng để test)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    logger = setup_logger("run_all", os.path.join(args.output_dir, "run_all.log"))

    logger.info("=" * 70)
    logger.info("NEWS CRAWLER PIPELINE")
    logger.info("=" * 70)
    logger.info(f"Start date    : {args.start}")
    logger.info(f"Enrich RSS    : {not args.no_enrich}")
    logger.info(f"Max pages/cat : {args.max_pages}")
    logger.info(f"Output dir    : {args.output_dir}")
    logger.info(f"RSS feeds     : {len(RSS_FEEDS)}")
    logger.info(f"HTML sites    : {list(SITE_CONFIG.keys())}")
    logger.info("=" * 70)

    if not args.html_only:
        logger.info(">>> RUNNING RSS CRAWLER")
        run_rss_crawler(
            output_path=os.path.join(args.output_dir, "rss_articles.jsonl"),
            checkpoint_path=os.path.join(args.output_dir, "checkpoint_rss.json"),
            log_path=os.path.join(args.output_dir, "rss_crawler.log"),
            start_date=args.start,
            enrich=not args.no_enrich,
            min_delay_per_domain=args.delay,
        )

    if not args.rss_only:
        logger.info(">>> RUNNING HTML CRAWLER")
        run_html_crawler(
            output_path=os.path.join(args.output_dir, "html_articles.jsonl"),
            checkpoint_path=os.path.join(args.output_dir, "checkpoint_html.json"),
            log_path=os.path.join(args.output_dir, "html_crawler.log"),
            start_date=args.start,
            max_pages_per_category=args.max_pages,
            min_delay_per_domain=args.delay,
        )

    if not args.skip_build:
        logger.info(">>> BUILDING FINAL DATASET")
        build_final_dataset(
            html_jsonl=os.path.join(args.output_dir, "html_articles.jsonl"),
            rss_jsonl=os.path.join(args.output_dir, "rss_articles.jsonl"),
            output_jsonl=os.path.join(args.output_dir, "articles_final.jsonl"),
            log_path=os.path.join(args.output_dir, "build_dataset.log"),
        )

    if not args.skip_cleanup:
        logger.info(">>> RUNNING CLEANUP (pre_dataset/01_cleanup.py)")
        run_cleanup(args.output_dir, logger)

    if not args.skip_ner:
        logger.info(">>> RUNNING NER (pre_dataset/02_ner.py)")
        run_ner(args.output_dir, args.ner_workers, args.ner_title_only, logger)

    logger.info("=" * 70)
    logger.info("PIPELINE FINISHED")
    logger.info("=" * 70)


def run_cleanup(output_dir: str, logger):
    """Invoke pre_dataset/01_cleanup.py qua subprocess.

    Cleanup = FULL REBUILD: đọc mọi articles_final*.jsonl + migrated_*.jsonl,
    ghi articles_cleaned*.jsonl với rotation ở 89MB. ~1-2 phút cho 150k bài.
    """
    script = os.path.join("pre_dataset", "01_cleanup.py")
    cmd = [
        sys.executable, script,
        "--input", os.path.join(output_dir, "articles_final*.jsonl"),
        "--input", os.path.join(output_dir, "migrated_*.jsonl"),
        "--output", os.path.join(output_dir, "articles_cleaned.jsonl"),
    ]
    logger.info(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        logger.error(f"Cleanup FAILED (exit code {result.returncode})")
        raise SystemExit(result.returncode)
    logger.info("  Cleanup OK")


def run_ner(output_dir: str, workers: int, title_only: bool, logger):
    """Invoke pre_dataset/02_ner.py qua subprocess.

    NER = INCREMENTAL: scan ID đã có trong articles_ner*.jsonl, chỉ NER phần mới.
    Lần chạy đầu (100k+ bài) có thể mất nhiều giờ → dùng --skip-ner trên GHA nếu cần.
    Daily run chỉ delta ~500-2000 bài → 5-30 phút.
    """
    script = os.path.join("pre_dataset", "02_ner.py")
    cmd = [
        sys.executable, script,
        "--input", os.path.join(output_dir, "articles_cleaned*.jsonl"),
        "--output", os.path.join(output_dir, "articles_ner.jsonl"),
        "--workers", str(workers),
    ]
    if title_only:
        cmd.append("--title-only")
    logger.info(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        logger.error(f"NER FAILED (exit code {result.returncode})")
        raise SystemExit(result.returncode)
    logger.info("  NER OK")


if __name__ == "__main__":
    main()
