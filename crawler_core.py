#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawler_core.py
---------------
Các tiện ích CHUNG cho toàn bộ hệ thống crawler:
- HTTP session + retry
- Per-domain rate limiter (tránh bị 429/block do dồn request vào 1 báo)
- Storage JSONL (append-safe, resume-safe)
- Dedupe (bộ nhớ trong) + load lại từ file khi resume
- Date/time utils (chuẩn hóa về ISO-8601 UTC)
- Hash ID ổn định từ URL
- Config & logging
"""

from __future__ import annotations

import os
import re
import ssl
import json
import glob
import html
import time
import random
import hashlib
import logging
import threading
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil import parser as dateparser


VN_TZ = timezone(timedelta(hours=7))


# =========================================================================
# SSL: cho phép legacy renegotiation (nhiều báo VN dùng TLS server cũ,
# gặp lỗi UNSAFE_LEGACY_RENEGOTIATION_DISABLED trên OpenSSL 3.x / Ubuntu 22.04+)
# =========================================================================

def _build_legacy_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # 0x4 = SSL_OP_LEGACY_SERVER_CONNECT; Python 3.12+ có hằng ssl.OP_LEGACY_SERVER_CONNECT
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


# Patch urllib (feedparser.parse dùng urllib internally)
_urllib_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_build_legacy_ssl_context())
)
urllib.request.install_opener(_urllib_opener)


class _LegacySSLAdapter(HTTPAdapter):
    """HTTPAdapter cho requests với SSL context cho phép legacy renegotiation."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _build_legacy_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = _build_legacy_ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


# =========================================================================
# Logging
# =========================================================================

def setup_logger(name: str, log_file: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """Logger vừa in ra console vừa ghi file (nếu có)."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Tránh add handler 2 lần khi re-run trong notebook
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


log = setup_logger("crawler_core")


# =========================================================================
# Article schema — dùng CHUNG cho cả RSS và HTML crawlers
# =========================================================================

@dataclass
class Article:
    """
    Schema thống nhất cho tất cả nguồn.
    source_type phân biệt bài đến từ RSS hay HTML crawler.
    """
    id: str                              # md5(url)
    url: str
    title: str
    content: str                         # đã strip boilerplate
    published_at: str                    # ISO-8601 UTC
    crawled_at: str                      # ISO-8601 UTC, thời điểm crawl
    source: str                          # tên báo chuẩn: "vnexpress", "dantri", ...
    source_domain: str                   # "vnexpress.net"
    source_type: str                     # "rss" | "html"
    category_raw: str = ""               # category gốc từ site ("Thế giới", "the-thao"...)
    category_normalized: str = ""        # map về taxonomy chung (sẽ làm ở postprocess)
    author: str = ""                     # tên tác giả (nếu có)
    language: str = "vi"
    keywords: List[str] = field(default_factory=list)
    entities: List[Dict[str, str]] = field(default_factory=list)  # sẽ populate ở bước NER
    content_length: int = 0
    has_full_content: bool = False       # True nếu content >= 500 chars

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# =========================================================================
# ID & URL utilities
# =========================================================================

def stable_id(url: str) -> str:
    """ID ổn định từ URL đã chuẩn hóa (md5)."""
    norm = normalize_url(url)
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Bỏ fragment, trailing slash để tránh duplicate 'cùng bài 2 URL khác nhau'."""
    if not url:
        return ""
    url = url.strip()
    # Bỏ fragment (#...)
    if "#" in url:
        url = url.split("#", 1)[0]
    # Bỏ trailing slash trừ khi là root
    p = urlparse(url)
    if p.path.endswith("/") and len(p.path) > 1:
        url = url[:-1]
    return url


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Map domain -> tên báo chuẩn
DOMAIN_TO_SOURCE = {
    "vnexpress.net": "vnexpress",
    "dantri.com.vn": "dantri",
    "tuoitre.vn": "tuoitre",
    "thanhnien.vn": "thanhnien",
    "vietnamnet.vn": "vietnamnet",
    "laodong.vn": "laodong",
    "nld.com.vn": "nld",
    "vietnamplus.vn": "vietnamplus",
    "soha.vn": "soha",
    "nhandan.vn": "nhandan",
    "baotintuc.vn": "baotintuc",
    "kienthuc.net.vn": "kienthuc",
    "znews.vn": "znews",
    "24h.com.vn": "24h",
}


def domain_to_source(domain: str) -> str:
    """Chuyển domain về tên báo chuẩn."""
    domain = domain.lower().replace("www.", "")
    if domain in DOMAIN_TO_SOURCE:
        return DOMAIN_TO_SOURCE[domain]
    # Subdomain: thitruong.nld.com.vn -> nld
    for known_domain, source in DOMAIN_TO_SOURCE.items():
        if domain.endswith("." + known_domain) or domain.endswith(known_domain):
            return source
    return domain  # fallback: trả về chính domain


# =========================================================================
# Date utilities
# =========================================================================

def to_iso_utc(raw: Optional[str]) -> Optional[str]:
    """Parse mọi format date về ISO-8601 UTC. Trả None nếu không parse được."""
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        dt = dateparser.parse(raw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            # Naive → giả định là giờ VN (các báo VN)
            dt = dt.replace(tzinfo=VN_TZ)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def iso_to_local_date(iso_utc: Optional[str]) -> Optional[str]:
    """ISO UTC → YYYY-MM-DD theo giờ VN."""
    if not iso_utc:
        return None
    try:
        dt = dateparser.parse(iso_utc)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(VN_TZ).date().isoformat()
    except Exception:
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =========================================================================
# HTTP session + Per-domain rate limiter
# =========================================================================

class PerDomainRateLimiter:
    """
    Rate limit theo domain (một domain chỉ được gọi tối đa 1 lần / delay giây).
    Thread-safe.
    """
    def __init__(self, min_delay: float = 0.5, jitter: float = 0.3):
        self.min_delay = min_delay
        self.jitter = jitter
        self._last_call: Dict[str, float] = defaultdict(float)
        self._locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._global_lock = threading.Lock()

    def wait(self, url: str):
        domain = get_domain(url)
        with self._global_lock:
            lock = self._locks[domain]
        with lock:
            now = time.time()
            last = self._last_call[domain]
            elapsed = now - last
            delay = self.min_delay + random.uniform(0, self.jitter)
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_call[domain] = time.time()


class HttpClient:
    """HTTP session dùng chung, có retry + per-domain rate limiting."""

    # Headers gần với Chrome thật. Sec-Fetch-* + Upgrade-Insecure-Requests + DNT
    # giúp né WAF (dantri trả 428 nếu thiếu chúng, nhất là khi crawl từ datacenter IP
    # như GitHub Actions). Accept-Encoding cho phép requests tự decode gzip/br.
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
        "Sec-Ch-Ua": '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    def __init__(
        self,
        timeout: int = 25,
        min_delay_per_domain: float = 0.5,
        jitter: float = 0.3,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.timeout = timeout
        self.rate_limiter = PerDomainRateLimiter(min_delay=min_delay_per_domain, jitter=jitter)

        self.session = requests.Session()
        headers = dict(self.DEFAULT_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        self.session.headers.update(headers)

        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=0.8,
            # 428 = Cloudflare anti-bot challenge ở dantri. 403 đôi khi cũng do WAF
            # nháy, retry sau backoff thường pass.
            status_forcelist=[403, 428, 429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = _LegacySSLAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """GET có rate limit + xử lý cookie challenge của laodong.vn."""
        self.rate_limiter.wait(url)
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True, **kwargs)
        except requests.RequestException as e:
            log.warning(f"GET failed {url}: {e}")
            return None

        # laodong.vn: cookie challenge
        if r.status_code == 200 and "document.cookie" in r.text[:1000] and len(r.content) < 1500:
            m = re.search(r'document\.cookie\s*=\s*"([^";]+)', r.text)
            if m:
                cookie_str = m.group(1)
                if "=" in cookie_str:
                    name, value = cookie_str.split("=", 1)
                    self.session.cookies.set(name.strip(), value.strip())
                    log.debug(f"Set challenge cookie for {url}")
                    time.sleep(0.5)
                    try:
                        r = self.session.get(url, timeout=self.timeout, allow_redirects=True, **kwargs)
                    except requests.RequestException as e:
                        log.warning(f"GET retry failed {url}: {e}")
                        return None

        if r.status_code >= 400:
            log.warning(f"HTTP {r.status_code} on {url}")
            return None
        return r

    def get_text(self, url: str, extra_headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        kwargs = {}
        if extra_headers:
            kwargs["headers"] = extra_headers
        r = self.get(url, **kwargs)
        return r.text if r is not None else None


# =========================================================================
# Storage — JSONL with resume support
# =========================================================================

ROTATE_THRESHOLD_BYTES = 90 * 1024 * 1024  # 90 MB — chừa buffer 10MB để chắc chắn dưới giới hạn 100MB của GitHub


class JsonlStore:
    """
    Append-only JSONL store. Mỗi dòng = 1 bài báo.
    - Thread-safe write (lock)
    - Load seen_ids từ file hiện tại + TẤT CẢ partition cũ (dedup cross-partition)
    - Tự rotate khi file > ROTATE_THRESHOLD_BYTES: rename thành {base}_{YYYY-MM}.jsonl
      (nếu trùng tháng → thêm counter _2, _3,…) rồi mở file mới.
    """

    def __init__(self, path: str, rotate_bytes: int = ROTATE_THRESHOLD_BYTES):
        safe_path = path.replace(":", "-")
        os.makedirs(os.path.dirname(safe_path) or ".", exist_ok=True)
        self.path = safe_path
        self.rotate_bytes = rotate_bytes
        self._lock = threading.Lock()
        self._seen_ids: Set[str] = set()
        self._seen_urls: Set[str] = set()
        self._maybe_rotate()
        self._load_all_partitions()

    def _partition_paths(self) -> List[str]:
        """Trả về danh sách các file partition đã rotate (không gồm file hiện tại)."""
        base, ext = os.path.splitext(self.path)
        return sorted(glob.glob(f"{base}_*{ext}"))

    def _next_rotated_name(self) -> str:
        """Sinh tên file partition: {base}_{YYYY-MM}[_N]{ext}."""
        base, ext = os.path.splitext(self.path)
        month = datetime.now().strftime("%Y-%m")
        candidate = f"{base}_{month}{ext}"
        n = 2
        while os.path.exists(candidate):
            candidate = f"{base}_{month}_{n}{ext}"
            n += 1
        return candidate

    def _maybe_rotate(self):
        """Nếu file hiện tại > threshold → rename thành partition, file mới sẽ trống."""
        if not os.path.exists(self.path):
            return
        size = os.path.getsize(self.path)
        if size < self.rotate_bytes:
            return
        target = self._next_rotated_name()
        os.rename(self.path, target)
        log.info(f"Rotated {self.path} → {target} ({size:,} bytes)")

    def _load_file(self, path: str):
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if "id" in obj:
                        self._seen_ids.add(obj["id"])
                    if "url" in obj:
                        self._seen_urls.add(obj["url"])
                    count += 1
                except json.JSONDecodeError:
                    continue
        log.info(f"Loaded {count:,} records from {path}")

    def _load_all_partitions(self):
        for p in self._partition_paths():
            self._load_file(p)
        if os.path.exists(self.path):
            self._load_file(self.path)

    def has_id(self, aid: str) -> bool:
        return aid in self._seen_ids

    def has_url(self, url: str) -> bool:
        return normalize_url(url) in self._seen_urls

    def append(self, article: Article) -> bool:
        """Ghi 1 bài. Trả False nếu đã tồn tại. Rotate lazily nếu file vượt ngưỡng."""
        with self._lock:
            if article.id in self._seen_ids:
                return False
            if os.path.exists(self.path) and os.path.getsize(self.path) >= self.rotate_bytes:
                target = self._next_rotated_name()
                os.rename(self.path, target)
                log.info(f"Rotated {self.path} → {target}")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(article.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
            self._seen_ids.add(article.id)
            self._seen_urls.add(article.url)
        return True

    def count(self) -> int:
        return len(self._seen_ids)


# =========================================================================
# Content cleaning — strip boilerplate cho từng báo
# =========================================================================

# Các pattern boilerplate phổ biến (sẽ strip ở đầu/cuối bài)
BOILERPLATE_PATTERNS = [
    # 24h.com.vn
    r"^Lưu bài viết thành công.*?Tin bài đã lưu",
    r"Nguồn:\s*\[Link nguồn\].*$",
    # Dân trí
    r"^\(Dân trí\)\s*-\s*",
    # Chung
    r"^TTO\s*-\s*",  # Tuổi Trẻ Online
    r"^TTXVN\s*-\s*",
]

_BOILERPLATE_REGEXES = [re.compile(p, re.DOTALL | re.IGNORECASE) for p in BOILERPLATE_PATTERNS]


def clean_content(text: str) -> str:
    """Strip boilerplate, decode HTML entities, chuẩn hóa whitespace."""
    if not text:
        return ""
    # Unescape HTML entities (&amp; → &, &quot; → ", &nbsp; → space, ...)
    # Chạy 2 lần để xử lý double-encoded (thường gặp trong JSON-LD articleBody)
    t = html.unescape(html.unescape(text))
    for rx in _BOILERPLATE_REGEXES:
        t = rx.sub("", t).strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t


# =========================================================================
# Category normalization
# =========================================================================

# Map category thô (nhiều dạng) về taxonomy chung
CATEGORY_MAP = {
    # sports
    "the-thao": "sports", "thể thao": "sports", "bong-da": "sports", "xe": "sports",
    "sea-games-32": "sports", "asiancup2019": "sports",
    # news / thoi-su
    "thoi-su": "news", "thời sự": "news", "tin-tuc-trong-ngay": "news",
    "xa-hoi": "news", "xã hội": "news", "tin-moi-nhat": "news", "tin tức": "news",
    # world
    "the-gioi": "world", "thế giới": "world", "quoc-te": "world", "quốc tế": "world",
    "asean": "world", "chau-a-tbd": "world", "chau-au": "world", "chau-my": "world",
    "chau-phi": "world", "trung-dong": "world",
    # politics
    "chinh-tri": "politics", "chính trị": "politics", "xay-dung-dang": "politics",
    "xa-luan": "politics",
    # business / economy
    "kinh-doanh": "business", "kinh-te": "business", "kinh doanh": "business",
    "kinh tế": "business", "kinh-doanh-tai-chinh": "business",
    "taichinh": "business", "chungkhoan": "business", "tai-chinh-bat-dong-san": "business",
    "bat-dong-san": "business", "thi-truong": "business",
    # entertainment
    "giai-tri": "entertainment", "giải trí": "entertainment", "van-hoa": "entertainment",
    "van-hoa-giai-tri": "entertainment", "van-hoa-van-nghe": "entertainment",
    "phim": "entertainment", "xuat-ban": "entertainment",
    # lifestyle
    "doi-song": "lifestyle", "đời sống": "lifestyle", "gia-dinh": "lifestyle",
    "ban-tre-cuoc-song": "lifestyle", "thoi-trang": "lifestyle",
    # law
    "phap-luat": "law", "pháp luật": "law",
    # education
    "giao-duc": "education", "giáo dục": "education", "giao-duc-du-hoc": "education",
    "giao-duc-khoa-hoc": "education", "giaoduc": "education",
    # health
    "suc-khoe": "health", "sức khỏe": "health", "y-te": "health", "yte": "health",
    # tech
    "cong-nghe": "tech", "công nghệ": "tech", "hi-tech": "tech", "so-hoa": "tech",
    "khoa-hoc": "tech", "khoa học": "tech", "ai-365": "tech",
    # travel
    "du-lich": "travel", "du lịch": "travel", "du-lich-xanh": "travel",
    # auto
    "oto-xe-may": "auto", "o-to-xe-may": "auto",
    # military
    "quan-su": "military", "quân sự": "military",
    # home
    "trang-chu": "home", "home": "home", "tin-moi": "home",
}


def normalize_category(raw: str) -> str:
    """Chuyển category thô về taxonomy chung. Nếu không match → 'other'."""
    if not raw:
        return "other"
    key = raw.strip().lower()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]
    # Thử match từng phần (slug có thể kết hợp nhiều từ)
    for k, v in CATEGORY_MAP.items():
        if k in key or key in k:
            return v
    return "other"


def extract_category_from_url(url: str) -> str:
    """Trích category từ path URL (fallback khi site không tag)."""
    try:
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        for part in path_parts[:2]:
            # Bỏ số trong slug (vd: thegioi-209 → thegioi)
            clean = re.sub(r"-\d+$", "", part)
            if normalize_category(clean) != "other":
                return clean
        return path_parts[0] if path_parts else ""
    except Exception:
        return ""


# =========================================================================
# Filter — loại bài rác
# =========================================================================

MIN_CONTENT_LENGTH_STRICT = 300   # cho HTML crawler — hạ từ 500 xuống 300 để thu nhiều dữ liệu hơn
MIN_CONTENT_LENGTH_LOOSE = 100    # cho RSS (có thể là summary)


def is_article_valid(
    title: str,
    content: str,
    published_at: str,
    min_content_len: int = MIN_CONTENT_LENGTH_LOOSE,
) -> Tuple[bool, str]:
    """Kiểm tra bài có hợp lệ không. Trả (is_valid, reason)."""
    if not title or not title.strip():
        return False, "empty_title"
    if not published_at:
        return False, "no_published_at"
    if not content or len(content) < min_content_len:
        return False, f"content_too_short({len(content) if content else 0})"
    return True, ""


# =========================================================================
# Checkpoint — resume after crash
# =========================================================================

class Checkpoint:
    """Lưu state (feed index / category URL đã crawl xong) để resume."""

    def __init__(self, path: str):
        self.path = path
        self.state: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except Exception:
                self.state = {}

    def is_done(self, key: str) -> bool:
        return self.state.get(key, {}).get("done") is True

    def mark_done(self, key: str, extra: Optional[Dict[str, Any]] = None):
        self.state[key] = {"done": True, "ts": utc_now_iso()}
        if extra:
            self.state[key].update(extra)
        self._save()

    def set(self, key: str, value: Any):
        self.state[key] = value
        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)


# =========================================================================
# Progress tracker (rất nhẹ, không cần tqdm)
# =========================================================================

class SimpleProgress:
    """Progress đơn giản in ra mỗi N bài."""

    def __init__(self, total: Optional[int] = None, log_every: int = 50, label: str = ""):
        self.total = total
        self.log_every = log_every
        self.label = label
        self.count = 0
        self.start = time.time()

    def tick(self, added: int = 1):
        self.count += added
        if self.count % self.log_every == 0:
            elapsed = time.time() - self.start
            rate = self.count / max(elapsed, 1e-6)
            tail = f"{self.count:,}"
            if self.total:
                pct = self.count / self.total * 100
                tail = f"{self.count:,}/{self.total:,} ({pct:.1f}%)"
            log.info(f"[{self.label}] {tail} @ {rate:.1f}/s")

    def done(self):
        elapsed = time.time() - self.start
        log.info(f"[{self.label}] DONE: {self.count:,} items in {elapsed:.1f}s")
