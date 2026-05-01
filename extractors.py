#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extractors.py
-------------
Site-specific extractors cho từng báo VN.

Mỗi extractor nhận HTML → trả về dict với các field:
    title, content, published_at, author, category_raw, keywords (list)

Nguyên tắc:
- Mỗi báo có selector RIÊNG (nhìn vào HTML structure thật của báo đó).
- Có fallback dùng `trafilatura` (nếu đã cài) hoặc selectors generic.
- Luôn ưu tiên JSON-LD nếu báo có nhúng (schema.org/NewsArticle) — đây là nguồn sạch nhất.
"""

from __future__ import annotations

import re
import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from crawler_core import (
    to_iso_utc,
    clean_content,
    extract_category_from_url,
    log,
)

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False


# =========================================================================
# JSON-LD extractor — báo nào có đều dùng được (chuẩn schema.org)
# =========================================================================

def extract_jsonld(soup: BeautifulSoup) -> Dict[str, Any]:
    """Tìm JSON-LD NewsArticle/Article và extract các field chuẩn."""
    out: Dict[str, Any] = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        txt = tag.string or tag.get_text() or ""
        if not txt.strip():
            continue
        # Một số báo ghi nhiều object trong 1 tag (dạng list)
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            # Thử fix trailing commas, control chars
            try:
                cleaned = re.sub(r",\s*([}\]])", r"\1", txt)
                data = json.loads(cleaned)
            except Exception:
                continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            t = item.get("@type", "")
            if isinstance(t, list):
                t = " ".join(t)
            if "Article" not in t and "NewsArticle" not in t:
                continue

            if "headline" in item and "title" not in out:
                out["title"] = str(item["headline"]).strip()
            if "datePublished" in item and "published_at" not in out:
                out["published_at"] = to_iso_utc(item["datePublished"]) or ""
            if "author" in item and "author" not in out:
                a = item["author"]
                if isinstance(a, dict):
                    out["author"] = a.get("name", "")
                elif isinstance(a, list) and a:
                    first = a[0]
                    out["author"] = first.get("name", "") if isinstance(first, dict) else str(first)
                else:
                    out["author"] = str(a)
            if "articleSection" in item and "category_raw" not in out:
                sec = item["articleSection"]
                if isinstance(sec, list):
                    sec = sec[0] if sec else ""
                out["category_raw"] = str(sec)
            if "keywords" in item and "keywords" not in out:
                kw = item["keywords"]
                if isinstance(kw, list):
                    out["keywords"] = [str(k).strip() for k in kw if k]
                elif isinstance(kw, str):
                    out["keywords"] = [k.strip() for k in kw.split(",") if k.strip()]
            if "articleBody" in item and "content" not in out:
                out["content"] = str(item["articleBody"])
    return out


# =========================================================================
# OpenGraph / meta fallback
# =========================================================================

def extract_meta_fallback(soup: BeautifulSoup) -> Dict[str, Any]:
    """Fallback dùng meta tags khi không có JSON-LD."""
    out: Dict[str, Any] = {}

    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        out["title"] = og_title["content"].strip()
    elif soup.title:
        out["title"] = soup.title.get_text(strip=True)

    for sel in [
        'meta[property="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[name="pubdate"]',
        'meta[name="publish-date"]',
    ]:
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            pub = to_iso_utc(tag["content"].strip())
            if pub:
                out["published_at"] = pub
                break

    if "published_at" not in out:
        ttag = soup.select_one("time[datetime]")
        if ttag and ttag.get("datetime"):
            pub = to_iso_utc(ttag["datetime"])
            if pub:
                out["published_at"] = pub

    section = soup.select_one('meta[property="article:section"]')
    if section and section.get("content"):
        out["category_raw"] = section["content"].strip()

    kw_tag = soup.select_one('meta[name="keywords"]')
    if kw_tag and kw_tag.get("content"):
        out["keywords"] = [k.strip() for k in kw_tag["content"].split(",") if k.strip()]

    author_tag = soup.select_one('meta[name="author"]') or soup.select_one('meta[property="article:author"]')
    if author_tag and author_tag.get("content"):
        out["author"] = author_tag["content"].strip()

    return out


# =========================================================================
# Body extractors — site-specific selectors
# =========================================================================

SITE_BODY_SELECTORS: Dict[str, List[str]] = {
    "vnexpress.net":   [".fck_detail", "article.fck_detail"],
    # Dantri (2026): chuyển sang Tailwind, layout mới dùng <article class="dt-flex ..."> hoặc
    # div.e-magazine__body cho e-magazine. Giữ selector cũ ở cuối làm fallback cho bài cũ.
    "dantri.com.vn":   ["div.e-magazine__body", "article.dt-flex", "article",
                        ".singular-content", ".dt-news__content", ".detail-content"],
    "tuoitre.vn":      [".detail-content", ".content-detail", "article.detail"],
    "thanhnien.vn":    [".detail-cmain", ".detail__content"],
    "vietnamnet.vn":   [".maincontent", ".content-detail"],
    "laodong.vn":      ["div.art-body", "article.article-detail", ".art-body", ".detail-content"],
    "nld.com.vn":      [".detail-content", "article .detail-cmain"],
    "vietnamplus.vn":  [".article-body", ".article__body", ".the-article-body"],
    "soha.vn":         [".detail-content-body", ".news-content"],
    "nhandan.vn":      [".article__body", ".detail-content"],
    "baotintuc.vn":    [".detail-content", ".article-body"],
    "kienthuc.net.vn": [".article-body", ".the-article-body"],
    "znews.vn":        [".the-article-body", "article .article-content"],
    "24h.com.vn":      [".text-conent", ".cate-24h-foot-home-detail-cmain", ".detail-news-content"],
}

# Các class/id cần REMOVE trước khi extract text (ads, related, social...)
NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe",
    ".ads", ".ad-container", ".advertisement",
    ".related-news", ".related-articles", ".box-tinlienquan",
    ".social-share", ".share-bar", ".share-box",
    ".comment", ".comments", "#comments",
    ".author-box", ".author-info",
    ".newsletter", ".subscribe",
    "figcaption",  # caption ảnh thường bị nhiễu
]


def _extract_body_with_selectors(soup: BeautifulSoup, selectors: List[str]) -> str:
    """Extract body text dùng danh sách selector theo thứ tự ưu tiên."""
    for sel in selectors:
        body = soup.select_one(sel)
        if not body:
            continue
        # Remove noise
        body_copy = BeautifulSoup(str(body), "lxml")
        for ns in NOISE_SELECTORS:
            for tag in body_copy.select(ns):
                tag.decompose()
        # Ưu tiên <p> — nội dung thật của bài
        paragraphs = body_copy.find_all("p")
        if paragraphs:
            parts = [p.get_text(" ", strip=True) for p in paragraphs]
            parts = [p for p in parts if p and len(p) > 10]
            if parts:
                return " ".join(parts)
        # Không có <p> → lấy toàn bộ text
        return body_copy.get_text(" ", strip=True)
    return ""


def _extract_body_trafilatura(html: str) -> str:
    """Fallback dùng trafilatura — thư viện trích nội dung thông minh."""
    if not HAS_TRAFILATURA:
        return ""
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
            favor_recall=True,
        )
        return text or ""
    except Exception:
        return ""


# =========================================================================
# Main entry — extract article from HTML
# =========================================================================

def extract_article(html: str, url: str) -> Dict[str, Any]:
    """
    Extract article info từ HTML của 1 URL.
    Trả dict các field; caller tự wrap vào Article dataclass.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")
    domain = urlparse(url).netloc.lower().replace("www.", "")

    # 1. Bắt đầu với JSON-LD (chuẩn nhất nếu có)
    out: Dict[str, Any] = extract_jsonld(soup)

    # 2. Bổ sung/override bằng meta tags nếu thiếu
    meta = extract_meta_fallback(soup)
    for k, v in meta.items():
        if not out.get(k):
            out[k] = v

    # 3. Body content — dùng selector site-specific
    if not out.get("content"):
        selectors = None
        for known_domain, sels in SITE_BODY_SELECTORS.items():
            if known_domain in domain:
                selectors = sels
                break
        if selectors:
            body = _extract_body_with_selectors(soup, selectors)
            if body:
                out["content"] = body

    # 4. Nếu vẫn không có content → trafilatura fallback
    if not out.get("content") or len(out.get("content", "")) < 200:
        body = _extract_body_trafilatura(html)
        if body and len(body) > len(out.get("content", "")):
            out["content"] = body

    # 5. Clean content (strip boilerplate)
    if "content" in out:
        out["content"] = clean_content(out["content"])

    # 6. Category fallback từ URL
    if not out.get("category_raw"):
        cat = extract_category_from_url(url)
        if cat:
            out["category_raw"] = cat

    # 7. Normalize keywords
    kw = out.get("keywords", [])
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",") if k.strip()]
    out["keywords"] = [str(k).strip() for k in kw if k]

    return out
