#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rss_crawler.py
--------------
Crawl tin tức qua RSS feeds + enrich full content từ URL gốc.

Flow:
1. Parse từng RSS feed → ra danh sách entry (url, title, pubDate, summary)
2. Filter theo ngày (lấy bài >= START_DATE)
3. Skip nếu URL đã có trong storage (resume-safe)
4. Fetch HTML từ URL gốc → extract article bằng site-specific extractor
5. Dùng content full, fallback về summary RSS nếu fetch failed
6. Ghi JSONL
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser
from bs4 import BeautifulSoup

from crawler_core import (
    Article,
    HttpClient,
    JsonlStore,
    Checkpoint,
    SimpleProgress,
    VN_TZ,
    stable_id,
    normalize_url,
    get_domain,
    domain_to_source,
    to_iso_utc,
    iso_to_local_date,
    utc_now_iso,
    clean_content,
    extract_category_from_url,
    is_article_valid,
    normalize_category,
    MIN_CONTENT_LENGTH_LOOSE,
    log,
)
from extractors import extract_article


# =========================================================================
# RSS feeds config
# =========================================================================

# Full list 137 feeds — giữ nguyên từ crawler cũ của bạn
RSS_FEEDS = [
    # VnExpress
    "https://vnexpress.net/rss/tin-moi-nhat.rss",
    "https://vnexpress.net/rss/thoi-su.rss",
    "https://vnexpress.net/rss/the-gioi.rss",
    "https://vnexpress.net/rss/kinh-doanh.rss",
    "https://vnexpress.net/rss/giai-tri.rss",
    "https://vnexpress.net/rss/the-thao.rss",
    "https://vnexpress.net/rss/phap-luat.rss",
    "https://vnexpress.net/rss/giao-duc.rss",
    "https://vnexpress.net/rss/suc-khoe.rss",
    "https://vnexpress.net/rss/gia-dinh.rss",
    "https://vnexpress.net/rss/du-lich.rss",
    "https://vnexpress.net/rss/khoa-hoc.rss",
    "https://vnexpress.net/rss/so-hoa.rss",
    "https://vnexpress.net/rss/oto-xe-may.rss",
    "https://vnexpress.net/rss/y-kien.rss",
    # Dân Trí — RSS hay bị HTTP 428 (anti-bot/Precondition Required).
    # Đã chuyển sang HTML crawler ở html_crawler.py SITE_CONFIG["dantri.com.vn"].
    # Tuổi Trẻ
    "https://tuoitre.vn/rss/tin-moi-nhat.rss",
    "https://tuoitre.vn/rss/thoi-su.rss",
    "https://tuoitre.vn/rss/the-gioi.rss",
    "https://tuoitre.vn/rss/phap-luat.rss",
    "https://tuoitre.vn/rss/kinh-doanh.rss",
    "https://tuoitre.vn/rss/giao-duc.rss",
    "https://tuoitre.vn/rss/the-thao.rss",
    "https://tuoitre.vn/rss/giai-tri.rss",
    "https://tuoitre.vn/rss/xe.rss",
    # Thanh Niên
    "https://thanhnien.vn/rss/home.rss",
    "https://thanhnien.vn/rss/thoi-su.rss",
    "https://thanhnien.vn/rss/the-gioi.rss",
    "https://thanhnien.vn/rss/kinh-te.rss",
    "https://thanhnien.vn/rss/van-hoa.rss",
    "https://thanhnien.vn/rss/the-thao.rss",
    "https://thanhnien.vn/rss/cong-nghe.rss",
    "https://thanhnien.vn/rss/gioi-tre.rss",
    # VietnamNet
    "https://vietnamnet.vn/rss/thoi-su.rss",
    "https://vietnamnet.vn/rss/the-gioi.rss",
    "https://vietnamnet.vn/rss/kinh-doanh.rss",
    "https://vietnamnet.vn/rss/giao-duc.rss",
    "https://vietnamnet.vn/rss/the-thao.rss",
    # Lao Động: đã được crawl qua html_crawler (xem SITE_CONFIG['laodong.vn'])
    # Người Lao Động (nld.com.vn)
    "https://nld.com.vn/rss/home.rss",
    "https://nld.com.vn/rss/thoi-su.rss",
    "https://nld.com.vn/rss/quoc-te.rss",
    "https://nld.com.vn/rss/lao-dong.rss",
    "https://nld.com.vn/rss/ban-doc.rss",
    "https://nld.com.vn/rss/kinh-te.rss",
    "https://nld.com.vn/rss/suc-khoe.rss",
    "https://nld.com.vn/rss/giao-duc-khoa-hoc.rss",
    "https://nld.com.vn/rss/phap-luat.rss",
    "https://nld.com.vn/rss/van-hoa-van-nghe.rss",
    "https://nld.com.vn/rss/giai-tri.rss",
    "https://nld.com.vn/rss/the-thao.rss",
    "https://nld.com.vn/rss/du-lich-xanh.rss",
    "https://nld.com.vn/rss/khoa-hoc.rss",
    # VietnamPlus
    "https://www.vietnamplus.vn/rss/home.rss",
    "https://www.vietnamplus.vn/rss/chinhtri-291.rss",
    "https://www.vietnamplus.vn/rss/thegioi-209.rss",
    "https://www.vietnamplus.vn/rss/kinhte-311.rss",
    "https://www.vietnamplus.vn/rss/xahoi-314.rss",
    "https://www.vietnamplus.vn/rss/doisong-320.rss",
    "https://www.vietnamplus.vn/rss/thethao-214.rss",
    # Soha
    "https://soha.vn/rss/home.rss",
    "https://soha.vn/rss/thoi-su-xa-hoi.rss",
    "https://soha.vn/rss/kinh-doanh.rss",
    "https://soha.vn/rss/quoc-te.rss",
    "https://soha.vn/rss/the-thao.rss",
    "https://soha.vn/rss/giai-tri.rss",
    "https://soha.vn/rss/phap-luat.rss",
    # Nhân Dân
    "https://nhandan.vn/rss/home.rss",
    "https://nhandan.vn/rss/chinhtri-1171.rss",
    "https://nhandan.vn/rss/kinhte-1185.rss",
    "https://nhandan.vn/rss/phapluat-1287.rss",
    "https://nhandan.vn/rss/du-lich-1257.rss",
    "https://nhandan.vn/rss/thegioi-1231.rss",
    "https://nhandan.vn/rss/thethao-1224.rss",
    # Báo Tin Tức
    "https://baotintuc.vn/tin-moi-nhat.rss",
    "https://baotintuc.vn/thoi-su.rss",
    "https://baotintuc.vn/the-gioi.rss",
    "https://baotintuc.vn/kinh-te.rss",
    "https://baotintuc.vn/xa-hoi.rss",
    "https://baotintuc.vn/phap-luat.rss",
    "https://baotintuc.vn/giao-duc.rss",
    "https://baotintuc.vn/van-hoa.rss",
    "https://baotintuc.vn/the-thao.rss",
    "https://baotintuc.vn/quan-su.rss",
    # Kiến Thức
    "https://kienthuc.net.vn/rss/home.rss",
    "https://kienthuc.net.vn/rss/chinh-tri-348.rss",
    "https://kienthuc.net.vn/rss/xa-hoi-349.rss",
    "https://kienthuc.net.vn/rss/the-gioi-350.rss",
    "https://kienthuc.net.vn/rss/quan-su-359.rss",
    "https://kienthuc.net.vn/rss/giai-tri-365.rss",
]


# =========================================================================
# Parse RSS entry
# =========================================================================

def _parse_entry_date(entry: Any) -> Optional[str]:
    """Extract ngày publish từ entry, trả ISO UTC."""
    # feedparser ưu tiên published_parsed
    if getattr(entry, "published_parsed", None):
        try:
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    # Fallback: parse string
    for attr in ("published", "updated", "pubDate"):
        val = entry.get(attr) if hasattr(entry, "get") else None
        if val:
            res = to_iso_utc(val)
            if res:
                return res
    return None


def _extract_rss_summary(entry: Any) -> str:
    """Lấy summary từ RSS entry (dùng khi enrich fail)."""
    raw = ""
    if getattr(entry, "content", None):
        c = entry.content
        if isinstance(c, list) and c:
            raw = c[0].get("value", "")
    if not raw:
        raw = entry.get("summary", "") or entry.get("description", "")
    if not raw:
        return ""
    # Strip HTML
    try:
        soup = BeautifulSoup(raw, "lxml")
        text = soup.get_text(" ", strip=True)
        return clean_content(text)
    except Exception:
        return clean_content(raw)


def _extract_rss_keywords(entry: Any) -> List[str]:
    """Lấy keywords/tags từ RSS entry."""
    keywords = []
    if getattr(entry, "tags", None):
        for tag in entry.tags:
            term = tag.get("term") if hasattr(tag, "get") else None
            if term:
                term = term.strip()
                if term and term not in keywords:
                    keywords.append(term)
    return keywords


# =========================================================================
# Main crawl logic
# =========================================================================

def build_article_from_rss(
    entry: Any,
    http: HttpClient,
    enrich: bool = True,
) -> Optional[Article]:
    """Build Article từ 1 RSS entry, có enrich full content nếu bật."""
    url = normalize_url(entry.get("link", ""))
    if not url:
        return None

    title = (entry.get("title") or "").strip()
    pub = _parse_entry_date(entry)
    if not pub:
        log.debug(f"No pub date for {url}")
        return None

    domain = get_domain(url)
    source = domain_to_source(domain)
    rss_keywords = _extract_rss_keywords(entry)
    rss_summary = _extract_rss_summary(entry)

    # --- ENRICH: fetch HTML và extract full ---
    content = ""
    category_raw = ""
    author = ""
    ld_keywords: List[str] = []

    if enrich:
        html = http.get_text(url)
        if html:
            extracted = extract_article(html, url)
            content = extracted.get("content", "")
            category_raw = extracted.get("category_raw", "")
            author = extracted.get("author", "")
            ld_keywords = extracted.get("keywords", [])
            # Nếu title từ JSON-LD/meta tốt hơn thì dùng
            if extracted.get("title") and len(extracted["title"]) > len(title):
                title = extracted["title"]

    # Fallback về RSS summary nếu không có content full
    if not content or len(content) < MIN_CONTENT_LENGTH_LOOSE:
        if rss_summary:
            content = rss_summary

    # Merge keywords: RSS tags + keywords từ meta/JSON-LD
    all_keywords = list(dict.fromkeys(rss_keywords + ld_keywords))  # dedup giữ order

    # Category fallback từ URL
    if not category_raw:
        category_raw = extract_category_from_url(url)

    valid, reason = is_article_valid(title, content, pub, min_content_len=MIN_CONTENT_LENGTH_LOOSE)
    if not valid:
        log.debug(f"Invalid article {url}: {reason}")
        return None

    return Article(
        id=stable_id(url),
        url=url,
        title=title,
        content=content,
        published_at=pub,
        crawled_at=utc_now_iso(),
        source=source,
        source_domain=domain,
        source_type="rss",
        category_raw=category_raw,
        category_normalized=normalize_category(category_raw),
        author=author,
        language="vi",
        keywords=all_keywords,
        entities=[],
        content_length=len(content),
        has_full_content=len(content) >= 500,
    )


def crawl_feed(
    feed_url: str,
    http: HttpClient,
    store: JsonlStore,
    start_date_iso: Optional[str],
    enrich: bool = True,
) -> Dict[str, int]:
    """Crawl 1 RSS feed. Trả stats."""
    stats = {"fetched": 0, "added": 0, "skipped_dup": 0, "skipped_old": 0, "skipped_invalid": 0}

    log.info(f"Fetching feed: {feed_url}")
    # Fetch qua HttpClient (có LegacySSLAdapter + browser UA) rồi feed bytes vào feedparser.
    # Cần vậy vì feedparser 6.0+ dùng SSL handler riêng → bypass urllib.install_opener patch,
    # dẫn đến lỗi UNSAFE_LEGACY_RENEGOTIATION_DISABLED trên nhiều báo VN (baotintuc, ...).
    # Đồng thời UA giống Chrome tránh WAF (dantri, ...) trả HTML thay vì RSS.
    resp = http.get(feed_url)
    if resp is None:
        log.warning(f"Feed fetch failed: {feed_url}")
        return stats
    feed = feedparser.parse(resp.content)
    if getattr(feed, "bozo", False) and not feed.entries:
        log.warning(f"Feed parse failed: {feed_url} — {feed.bozo_exception}")
        return stats

    entries = feed.entries or []
    log.info(f"Feed has {len(entries)} entries: {feed_url}")

    for entry in entries:
        stats["fetched"] += 1
        url = normalize_url(entry.get("link", ""))
        if not url:
            continue

        # Check dup trước khi enrich (tiết kiệm request)
        if store.has_url(url):
            stats["skipped_dup"] += 1
            continue

        # Check ngày trước khi enrich
        pub_iso = _parse_entry_date(entry)
        if start_date_iso and pub_iso:
            pub_local = iso_to_local_date(pub_iso)
            start_local = iso_to_local_date(start_date_iso)
            if pub_local and start_local and pub_local < start_local:
                stats["skipped_old"] += 1
                continue

        article = build_article_from_rss(entry, http, enrich=enrich)
        if article is None:
            stats["skipped_invalid"] += 1
            continue

        if store.append(article):
            stats["added"] += 1
        else:
            stats["skipped_dup"] += 1

    return stats


def run_rss_crawler(
    feeds: Optional[List[str]] = None,
    output_path: str = "data/rss_articles.jsonl",
    checkpoint_path: str = "data/checkpoint_rss.json",
    log_path: str = "data/rss_crawler.log",
    start_date: Optional[str] = None,  # "YYYY-MM-DD" — chỉ lấy bài >= ngày này
    enrich: bool = True,
    min_delay_per_domain: float = 0.8,
):
    """Entry point để chạy RSS crawler."""
    # Wire up logger trước tất cả
    logger = __import__("crawler_core").setup_logger("rss_crawler", log_path)
    logger.info("=" * 60)
    logger.info("RSS CRAWLER STARTED")
    logger.info("=" * 60)

    feeds = feeds or RSS_FEEDS
    http = HttpClient(min_delay_per_domain=min_delay_per_domain)
    store = JsonlStore(output_path)
    checkpoint = Checkpoint(checkpoint_path)

    start_iso = None
    if start_date:
        start_iso = to_iso_utc(start_date + "T00:00:00")

    total = {"fetched": 0, "added": 0, "skipped_dup": 0, "skipped_old": 0, "skipped_invalid": 0}
    progress = SimpleProgress(total=len(feeds), log_every=5, label="rss_feeds")

    # Gắn bucket 6 tiếng UTC vào checkpoint key → chỉ skip feed đã crawl TRONG 6H GẦN ĐÂY.
    # Sang bucket mới (00/06/12/18 UTC) key đổi → crawler quét lại; dedup vẫn qua JsonlStore.
    _now = datetime.now(timezone.utc)
    bucket = _now.strftime("%Y-%m-%d") + f"T{(_now.hour // 6) * 6:02d}"

    for i, feed_url in enumerate(feeds, 1):
        ckpt_key = f"feed:{bucket}:{feed_url}"
        if checkpoint.is_done(ckpt_key):
            logger.info(f"[{i}/{len(feeds)}] SKIP (already done in last 6h): {feed_url}")
            progress.tick()
            continue

        try:
            stats = crawl_feed(feed_url, http, store, start_iso, enrich=enrich)
            for k, v in stats.items():
                total[k] += v
            logger.info(
                f"[{i}/{len(feeds)}] OK {feed_url} — "
                f"added={stats['added']} dup={stats['skipped_dup']} "
                f"old={stats['skipped_old']} invalid={stats['skipped_invalid']}"
            )
            checkpoint.mark_done(ckpt_key, extra=stats)
        except Exception as e:
            logger.exception(f"[{i}/{len(feeds)}] FAIL {feed_url}: {e}")

        progress.tick()

    progress.done()
    logger.info("=" * 60)
    logger.info(f"FINAL STATS: {total}")
    logger.info(f"Total records in store: {store.count():,}")
    logger.info(f"Output: {output_path}")
    logger.info("=" * 60)


if __name__ == "__main__":
    run_rss_crawler(
        start_date="2026-01-14",
        enrich=True,
    )
