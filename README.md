# News Crawler — Hệ thống crawl tin tức tiếng Việt

Hệ thống modular để crawl tin tức từ 14 báo lớn VN, dùng cho đề tài
**"Hệ thống lưu trữ và phân tích xu hướng tin tức với NoSQL + Time-series DB"**.

## 🗂 Cấu trúc

```
news_crawler/
├── crawler_core.py    # Module CHUNG: HTTP client, storage, rate limit, dedupe, logging
├── extractors.py      # Site-specific HTML extractors (JSON-LD + meta + selectors + trafilatura)
├── rss_crawler.py     # RSS crawler với enrich full content
├── html_crawler.py    # HTML category-page crawler (24h, Znews, Lao Động)
├── run_all.py         # Entry point — chạy cả 2
├── migrate_csv.py     # Chuyển CSV cũ → JSONL mới (không mất data)
├── requirements.txt
└── data/              # Output (tự tạo)
    ├── rss_articles.jsonl
    ├── html_articles.jsonl
    ├── checkpoint_rss.json      # để resume
    ├── checkpoint_html.json
    └── *.log                    # log file
```

## 🚀 Cài đặt

```bash
pip install -r requirements.txt
```

## 📋 Cách dùng

### 1. Chạy mới hoàn toàn
```bash
python run_all.py --start 2026-01-14
```

### 2. Chỉ crawl RSS (nhanh hơn, ~20-30 phút)
```bash
python run_all.py --rss-only --start 2026-01-14
```

### 3. Crawl RSS không enrich (CỰC nhanh nhưng content ngắn)
```bash
python run_all.py --rss-only --no-enrich
```

### 4. Migrate dữ liệu CSV cũ của bạn sang JSONL
```bash
python migrate_csv.py \
  --csv /path/to/24h_html_categories_vi.csv:html \
  --csv /path/to/znews_html_categories_vi.csv:html \
  --csv /path/to/rss_feed_articles_v2.csv:rss \
  --output data/migrated_articles.jsonl
```

Migration **tự động fix** các vấn đề của dataset cũ:
- Fix `source.name` bị nhầm tên tác giả → tách thành `source` (từ domain) + `author`
- Clean boilerplate "Lưu bài viết thành công..."
- Category `trang-chu` → fallback parse từ URL
- Parse keywords từ `"a|b|c"` → list
- Tạo các field mới: `source_domain`, `source_type`, `content_length`, `has_full_content`

### 5. Resume sau khi crash
Crawler tự động resume — chỉ cần chạy lại cùng lệnh. Checkpoint lưu trong `data/checkpoint_*.json`, storage lưu trong `data/*.jsonl` (không ghi lại bài đã có).

## 📝 Schema output (mỗi dòng JSONL)

```json
{
  "id": "a941805bd131e05bae9a55c1a15d7f62",
  "url": "https://vnexpress.net/...",
  "title": "...",
  "content": "...",                    // đã strip boilerplate
  "published_at": "2026-02-03T13:45:00+00:00",
  "crawled_at": "2026-04-18T07:30:00+00:00",
  "source": "vnexpress",                // tên báo chuẩn hóa
  "source_domain": "vnexpress.net",
  "source_type": "rss",                 // "rss" | "html"
  "category_raw": "Thế giới",
  "category_normalized": "world",
  "author": "Đức Hoàng",
  "language": "vi",
  "keywords": ["Starlink", "Ukraine", "Nga"],
  "entities": [],                       // populate bằng NER ở bước sau
  "content_length": 3218,
  "has_full_content": true
}
```

## 🎯 Các cải tiến so với crawler cũ

| Vấn đề cũ | Giải pháp mới |
|---|---|
| Path CSV có dấu `/` (`"..._from_25/02/2026.csv"`) | JsonlStore tự sanitize path |
| `source.name` bị nhầm tên author | Tách thành 2 field: `source` (từ domain) + `author` |
| `entities` luôn rỗng | Field có, để trống → chạy NER bước sau bằng `underthesea` |
| Content có boilerplate "Lưu bài viết..." | `clean_content()` với regex patterns |
| Selector body tổng quát → miss content VnExpress, Soha... | Site-specific selectors + trafilatura fallback |
| Code duplicate 4 lần (4 crawler) | Module chung `crawler_core.py` |
| Không resume sau crash | Checkpoint + JsonlStore load seen_ids |
| Rate limit global → dồn request vào 1 báo | `PerDomainRateLimiter` |
| Không có JSON-LD extractor | `extract_jsonld()` — nguồn sạch nhất |

## 🧪 Test nhanh

```bash
# Chạy thử với 1 feed, 1 bài:
python -c "
from rss_crawler import crawl_feed
from crawler_core import HttpClient, JsonlStore
http = HttpClient()
store = JsonlStore('data/test.jsonl')
stats = crawl_feed('https://vnexpress.net/rss/thoi-su.rss', http, store, None, enrich=True)
print(stats)
"
```

## 📊 Ước tính thời gian chạy

| Scenario | RSS feeds | Enrich | Thời gian ước tính |
|---|---|---|---|
| RSS no-enrich | ~100 | ❌ | 5-10 phút |
| RSS + enrich full content | ~100 | ✅ | 2-4 giờ |
| HTML category crawl | 3 sites × ~10 cats | — | 1-2 giờ |
| **Full run** | — | ✅ | **3-6 giờ** |

## 🔜 Bước tiếp theo (ngoài phạm vi crawler)

1. **NER**: chạy `underthesea.ner()` lên field `content` → populate `entities`
2. **Dedup cross-source**: MinHash/SimHash để gộp bài cùng sự kiện từ nhiều báo
3. **Index vào Elasticsearch + ClickHouse**: bulk import JSONL
4. **Generate synthetic data** cho benchmark (blow up lên 1M-10M events)
