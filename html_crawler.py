#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
html_crawler.py
---------------
Crawl tin tức qua category pages (cho các site không có RSS tốt / muốn full content).

Flow:
1. Với mỗi category URL:
   - Duyệt từ trang 1 → N (hoặc đến khi hết bài mới hoặc gặp bài quá cũ)
   - Trên mỗi trang: extract danh sách article URLs
   - Với mỗi URL: fetch HTML → extract article → ghi JSONL
2. Hỗ trợ 24h, Znews, Lao Động (và dễ thêm site khác qua SITE_CONFIG)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler_core import (
    Article,
    HttpClient,
    JsonlStore,
    Checkpoint,
    SimpleProgress,
    stable_id,
    normalize_url,
    get_domain,
    domain_to_source,
    iso_to_local_date,
    utc_now_iso,
    normalize_category,
    is_article_valid,
    to_iso_utc,
    MIN_CONTENT_LENGTH_STRICT,
    setup_logger,
    log,
)
from extractors import extract_article


# =========================================================================
# Site configs — mỗi site có rule khác nhau để đi qua các trang
# =========================================================================

SITE_CONFIG: Dict[str, Dict[str, Any]] = {
    # 24h: site này dùng chủ yếu qua RSS (xem rss_crawler.py). HTML category
    # pagination của 24h không ổn định nên mặc định bỏ qua trong html_crawler.
    # Giữ config ở đây để tham khảo nhưng không enable mặc định.
    "24h.com.vn": {
        "category_pages": [
            "https://www.24h.com.vn/tin-tuc-trong-ngay-c46.html",
            "https://www.24h.com.vn/bong-da-c48.html",
            "https://www.24h.com.vn/the-thao-c101.html",
            "https://www.24h.com.vn/thoi-trang-c78.html",
            "https://www.24h.com.vn/hi-tech-c66.html",
            "https://www.24h.com.vn/tai-chinh-bat-dong-san-c161.html",
            "https://www.24h.com.vn/phim-c74.html",
            "https://www.24h.com.vn/giao-duc-du-hoc-c216.html",
            "https://www.24h.com.vn/ban-tre-cuoc-song-c64.html",
        ],
        "pagination_template": "{base}-p{page}.html",
        "article_url_regex": re.compile(r"-c\d+a\d+\.html"),
    },
    # znews: trang 2 dạng /<slug>/trang{N}.html, article URL chứa -post{ID}.html
    "znews.vn": {
        "category_pages": [
            "https://znews.vn/xuat-ban.html",
            "https://znews.vn/kinh-doanh-tai-chinh.html",
            "https://znews.vn/suc-khoe.html",
            "https://znews.vn/the-thao.html",
            "https://znews.vn/doi-song.html",
            "https://znews.vn/cong-nghe.html",
            "https://znews.vn/giai-tri.html",
        ],
        "pagination_template": "{base}/trang{page}.html",
        "article_url_regex": re.compile(r"-post\d+\.html"),
    },
    # laodong: pagination qua query ?page=N, article URL kết thúc bằng .ldo
    "laodong.vn": {
        "category_pages": [
            "https://laodong.vn/thoi-su/",
            "https://laodong.vn/the-gioi/",
            "https://laodong.vn/xa-hoi/",
            "https://laodong.vn/phap-luat/",
            "https://laodong.vn/kinh-doanh/",
            "https://laodong.vn/bat-dong-san/",
            "https://laodong.vn/van-hoa/",
            "https://laodong.vn/giao-duc/",
            "https://laodong.vn/the-thao/",
            "https://laodong.vn/suc-khoe/",
            "https://laodong.vn/cong-nghe/",
            "https://laodong.vn/xe/",
            "https://laodong.vn/du-lich/",
        ],
        "pagination_template": "{base}?page={page}",
        "article_url_regex": re.compile(r"-\d+\.ldo$"),
    },
    # dantri: category .htm, pagination /trang-N.htm, article URL kết thúc bằng -<14+ digits>.htm
    # (dantri RSS hay bị HTTP 428 do anti-bot — dùng HTML crawler thay thế).
    "dantri.com.vn": {
        "category_pages": [
            "https://dantri.com.vn/tin-moi-nhat.htm",
            "https://dantri.com.vn/the-gioi.htm",
            "https://dantri.com.vn/thoi-su.htm",
            "https://dantri.com.vn/phap-luat.htm",
            "https://dantri.com.vn/suc-khoe.htm",
            "https://dantri.com.vn/doi-song.htm",
            "https://dantri.com.vn/du-lich.htm",
            "https://dantri.com.vn/kinh-doanh.htm",
            "https://dantri.com.vn/bat-dong-san.htm",
            "https://dantri.com.vn/the-thao.htm",
            "https://dantri.com.vn/giai-tri.htm",
            "https://dantri.com.vn/giao-duc.htm",
            "https://dantri.com.vn/o-to-xe-may.htm",
            "https://dantri.com.vn/noi-vu.htm",
            "https://dantri.com.vn/cong-nghe.htm",
        ],
        "pagination_template": "{base}/trang-{page}.htm",
        "article_url_regex": re.compile(r"-\d{10,}\.htm$"),
    },
}

# Sites được bật mặc định (24h dùng RSS, không qua HTML crawler)
DEFAULT_SITES = ["znews.vn", "laodong.vn", "dantri.com.vn"]


# =========================================================================
# Pagination URL generator
# =========================================================================

def _make_pagination_url(category_url: str, page: int, template: str) -> str:
    """Dựng URL của trang N từ category URL gốc."""
    if page == 1:
        return category_url
    # Strip extension nếu có (.html: znews/24h, .htm: dantri); else strip trailing /
    if category_url.endswith(".html"):
        base = category_url[:-len(".html")]
    elif category_url.endswith(".htm"):
        base = category_url[:-len(".htm")]
    else:
        # laodong: https://laodong.vn/thoi-su/ → base = https://laodong.vn/thoi-su
        base = category_url.rstrip("/")
    return template.format(base=base, page=page)


# =========================================================================
# Extract article URLs from a category page
# =========================================================================

def extract_article_urls(html: str, base_url: str, url_regex: re.Pattern) -> List[str]:
    """Từ trang category, extract tất cả URL bài báo khớp pattern."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    urls = set()
    domain = get_domain(base_url)

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        # Resolve relative URL
        full = urljoin(base_url, href)
        # Strip query params (vd utm_source) để match regex dạng \.ldo$ chính xác
        full = full.split("?", 1)[0]
        # Chỉ giữ URL cùng domain
        if get_domain(full) != domain:
            continue
        # Match article pattern
        if url_regex.search(full):
            urls.add(normalize_url(full))

    return list(urls)


# =========================================================================
# Process a single article URL
# =========================================================================

def process_article_url(
    url: str,
    http: HttpClient,
    store: JsonlStore,
    start_date: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """Fetch + extract + store 1 article. Trả (added, reason, pub_local_date)."""
    if store.has_url(url):
        return False, "dup", ""

    html = http.get_text(url)
    if not html:
        return False, "fetch_failed", ""

    data = extract_article(html, url)
    title = data.get("title", "")
    content = data.get("content", "")
    pub = data.get("published_at", "")
    pub_local = iso_to_local_date(pub) or ""

    # Bài cũ hơn start_date → bỏ qua (không ghi, không tính là invalid)
    if start_date and pub_local and pub_local < start_date:
        return False, "too_old", pub_local

    valid, reason = is_article_valid(title, content, pub, min_content_len=MIN_CONTENT_LENGTH_STRICT)
    if not valid:
        return False, f"invalid:{reason}", pub_local

    domain = get_domain(url)
    article = Article(
        id=stable_id(url),
        url=url,
        title=title,
        content=content,
        published_at=pub,
        crawled_at=utc_now_iso(),
        source=domain_to_source(domain),
        source_domain=domain,
        source_type="html",
        category_raw=data.get("category_raw", ""),
        category_normalized=normalize_category(data.get("category_raw", "")),
        author=data.get("author", ""),
        language="vi",
        keywords=data.get("keywords", []),
        entities=[],
        content_length=len(content),
        has_full_content=len(content) >= 500,
    )

    added = store.append(article)
    return added, "added" if added else "dup", pub_local


# =========================================================================
# Crawl one category — walking pages
# =========================================================================

def crawl_category(
    category_url: str,
    pagination_template: str,
    url_regex: re.Pattern,
    http: HttpClient,
    store: JsonlStore,
    start_date: Optional[str] = None,
    max_pages: int = 50,
    consecutive_empty_stop: int = 3,
) -> Dict[str, int]:
    """
    Crawl 1 category, đi từ trang 1 → max_pages.
    Dừng nếu:
      - Gặp `consecutive_empty_stop` trang liên tiếp không có URL mới nào
      - Gặp bài có pub_date < start_date (dự kiến tất cả bài tiếp theo cũng cũ hơn)
    """
    stats = {"added": 0, "dup": 0, "invalid": 0, "fetch_failed": 0, "too_old": 0, "pages_visited": 0}
    consecutive_empty = 0
    all_seen_on_this_category: Set[str] = set()

    for page in range(1, max_pages + 1):
        page_url = _make_pagination_url(category_url, page, pagination_template)
        log.info(f"Page {page}: {page_url}")
        html = http.get_text(page_url)
        stats["pages_visited"] += 1
        if not html:
            consecutive_empty += 1
            if consecutive_empty >= consecutive_empty_stop:
                log.info(f"Stopping category after {consecutive_empty} empty pages")
                break
            continue

        urls = extract_article_urls(html, page_url, url_regex)
        new_urls = [u for u in urls if u not in all_seen_on_this_category]
        if not new_urls:
            consecutive_empty += 1
            if consecutive_empty >= consecutive_empty_stop:
                log.info(f"Stopping category: no new URLs in {consecutive_empty} pages")
                break
            continue

        consecutive_empty = 0
        all_seen_on_this_category.update(new_urls)

        # Theo dõi page này có bài nào >= start_date không (nếu không → toàn bài cũ → dừng)
        page_has_valid_date = False
        page_has_fresh_article = False

        for url in new_urls:
            added, reason, pub_local = process_article_url(url, http, store, start_date=start_date)
            if reason == "added":
                stats["added"] += 1
            elif reason == "too_old":
                stats["too_old"] += 1
            elif reason.startswith("invalid"):
                stats["invalid"] += 1
            elif reason == "fetch_failed":
                stats["fetch_failed"] += 1
            else:
                stats["dup"] += 1

            if pub_local:
                page_has_valid_date = True
                if not start_date or pub_local >= start_date:
                    page_has_fresh_article = True

        # Tất cả bài trên page này đều có pub_date < start_date → dừng crawl category
        if start_date and page_has_valid_date and not page_has_fresh_article:
            log.info(
                f"Stopping category {category_url}: all articles on page {page} older than {start_date}"
            )
            break

        log.debug(f"Page {page} stats so far: {stats}")

    return stats


# =========================================================================
# Entry point
# =========================================================================

def run_html_crawler(
    sites: Optional[List[str]] = None,
    output_path: str = "data/html_articles.jsonl",
    checkpoint_path: str = "data/checkpoint_html.json",
    log_path: str = "data/html_crawler.log",
    start_date: Optional[str] = None,
    max_pages_per_category: int = 50,
    min_delay_per_domain: float = 0.8,
):
    """Chạy HTML crawler cho các site trong sites (default: tất cả)."""
    logger = setup_logger("html_crawler", log_path)
    logger.info("=" * 60)
    logger.info("HTML CRAWLER STARTED")
    logger.info("=" * 60)

    if sites is None:
        sites = list(DEFAULT_SITES)

    http = HttpClient(min_delay_per_domain=min_delay_per_domain)
    store = JsonlStore(output_path)
    checkpoint = Checkpoint(checkpoint_path)

    grand_total = {"added": 0, "dup": 0, "invalid": 0, "fetch_failed": 0, "too_old": 0, "pages_visited": 0}

    # Gắn bucket 6 tiếng UTC vào checkpoint key → chỉ skip category đã crawl TRONG 6H GẦN ĐÂY.
    # Sang bucket mới (00/06/12/18 UTC) key đổi → crawler chạy lại; dedup vẫn qua JsonlStore.
    _now = datetime.now(timezone.utc)
    bucket = _now.strftime("%Y-%m-%d") + f"T{(_now.hour // 6) * 6:02d}"

    for site_domain in sites:
        if site_domain not in SITE_CONFIG:
            logger.warning(f"No config for site: {site_domain}")
            continue
        config = SITE_CONFIG[site_domain]
        logger.info(f">>> SITE: {site_domain}")
        for cat_url in config["category_pages"]:
            ckpt_key = f"cat:{bucket}:{cat_url}"
            if checkpoint.is_done(ckpt_key):
                logger.info(f"  SKIP (already done in last 6h): {cat_url}")
                continue
            try:
                stats = crawl_category(
                    category_url=cat_url,
                    pagination_template=config["pagination_template"],
                    url_regex=config["article_url_regex"],
                    http=http,
                    store=store,
                    start_date=start_date,
                    max_pages=max_pages_per_category,
                )
                for k, v in stats.items():
                    grand_total[k] += v
                logger.info(f"  OK {cat_url}: {stats}")
                checkpoint.mark_done(ckpt_key, extra=stats)
            except Exception as e:
                logger.exception(f"  FAIL {cat_url}: {e}")

    logger.info("=" * 60)
    logger.info(f"FINAL STATS: {grand_total}")
    logger.info(f"Total records in store: {store.count():,}")
    logger.info(f"Output: {output_path}")
    logger.info("=" * 60)


if __name__ == "__main__":
    run_html_crawler(
        start_date="2026-01-14",
        max_pages_per_category=50,
    )
