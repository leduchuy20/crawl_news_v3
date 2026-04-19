#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dataset.py
----------------
Gộp 2 file crawl (html_articles.jsonl + rss_articles.jsonl) → articles_final.jsonl.

Quy tắc:
- Dedup theo `id` (md5 URL). Nếu 1 bài có ở cả 2 file → giữ bản có content DÀI HƠN
  (thường là bản HTML), đồng thời merge `keywords` thành union.
- Chỉ append bài MỚI vào articles_final.jsonl (không overwrite). Bài đã có giữ nguyên.
- Re-validate qua is_article_valid với threshold LOOSE (100 chars).
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import fields
from typing import Any, Dict, Iterator, List, Optional

from crawler_core import (
    Article,
    JsonlStore,
    clean_content,
    is_article_valid,
    MIN_CONTENT_LENGTH_LOOSE,
    setup_logger,
)

_ARTICLE_FIELDS = {f.name for f in fields(Article)}

log = setup_logger("build_dataset")  # console-only fallback; file handler thêm trong build_final_dataset


def _expand_partitions(path: str) -> List[str]:
    """Trả về file chính + mọi partition {base}_*{ext} đã rotate (sort theo tên)."""
    base, ext = os.path.splitext(path)
    parts = sorted(glob.glob(f"{base}_*{ext}"))
    if os.path.exists(path):
        parts.append(path)
    return parts


def _read_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    for p in _expand_partitions(path):
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _dict_to_article(d: Dict[str, Any]) -> Article:
    """Dựng Article từ dict (đã crawl), re-clean content + fill default."""
    kwargs = {k: d.get(k) for k in _ARTICLE_FIELDS}
    for k in ("id", "url", "title", "content", "published_at", "crawled_at",
              "source", "source_domain", "source_type", "category_raw",
              "category_normalized", "author"):
        if kwargs.get(k) is None:
            kwargs[k] = ""
    # Re-clean để unescape &amp;/&quot;/... cho data crawl từ phiên bản cũ
    kwargs["title"] = clean_content(kwargs["title"])
    kwargs["content"] = clean_content(kwargs["content"])
    if kwargs.get("language") is None:
        kwargs["language"] = "vi"
    kws = kwargs.get("keywords") or []
    kwargs["keywords"] = [clean_content(k) for k in kws if k and clean_content(k)]
    if kwargs.get("entities") is None:
        kwargs["entities"] = []
    content = kwargs.get("content") or ""
    kwargs["content_length"] = len(content)
    kwargs["has_full_content"] = len(content) >= 500
    return Article(**kwargs)


def _merge(base: Dict[str, Any], other: Dict[str, Any]) -> Dict[str, Any]:
    """Giữ bản có content dài hơn, union keywords."""
    if len(other.get("content", "") or "") > len(base.get("content", "") or ""):
        winner, loser = other, base
    else:
        winner, loser = base, other
    merged = dict(winner)
    merged["keywords"] = list(dict.fromkeys(
        (winner.get("keywords") or []) + (loser.get("keywords") or [])
    ))
    content = merged.get("content") or ""
    merged["content_length"] = len(content)
    merged["has_full_content"] = len(content) >= 500
    return merged


def build_final_dataset(
    html_jsonl: str = "data/html_articles.jsonl",
    rss_jsonl: str = "data/rss_articles.jsonl",
    output_jsonl: str = "data/articles_final.jsonl",
    log_path: Optional[str] = "data/build_dataset.log",
) -> Dict[str, int]:
    """Gộp 2 file, dedup, append bài mới vào output. Trả stats."""
    # Attach file handler cho logger "build_dataset" nếu có log_path
    if log_path:
        import logging
        lg = logging.getLogger("build_dataset")
        has_file = any(isinstance(h, logging.FileHandler) and
                       os.path.abspath(getattr(h, "baseFilename", "")) == os.path.abspath(log_path)
                       for h in lg.handlers)
        if not has_file:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(fh)

    log.info("=" * 60)
    log.info("BUILD FINAL DATASET")
    log.info("=" * 60)
    log.info(f"Input HTML : {html_jsonl}")
    log.info(f"Input RSS  : {rss_jsonl}")
    log.info(f"Output     : {output_jsonl}")

    store = JsonlStore(output_jsonl)

    # Aggregate by id (HTML first vì content chuẩn hơn, rồi RSS merge keywords)
    by_id: Dict[str, Dict[str, Any]] = {}
    for path in (html_jsonl, rss_jsonl):
        for obj in _read_jsonl(path):
            aid = obj.get("id")
            if not aid:
                continue
            if aid in by_id:
                by_id[aid] = _merge(by_id[aid], obj)
            else:
                by_id[aid] = obj
    log.info(f"Total unique articles across sources: {len(by_id):,}")

    stats = {"total": len(by_id), "added": 0, "already_in_output": 0, "invalid": 0}
    for aid, obj in by_id.items():
        if store.has_id(aid):
            stats["already_in_output"] += 1
            continue
        valid, reason = is_article_valid(
            obj.get("title", ""),
            obj.get("content", ""),
            obj.get("published_at", ""),
            min_content_len=MIN_CONTENT_LENGTH_LOOSE,
        )
        if not valid:
            stats["invalid"] += 1
            continue
        if store.append(_dict_to_article(obj)):
            stats["added"] += 1

    log.info(f"STATS: {stats}")
    log.info(f"Total records in {output_jsonl}: {store.count():,}")
    log.info("=" * 60)
    return stats


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Merge crawled JSONL → final dataset")
    p.add_argument("--html", default="data/html_articles.jsonl")
    p.add_argument("--rss", default="data/rss_articles.jsonl")
    p.add_argument("--output", default="data/articles_final.jsonl")
    p.add_argument("--log", default="data/build_dataset.log")
    args = p.parse_args()
    build_final_dataset(args.html, args.rss, args.output, log_path=args.log)
