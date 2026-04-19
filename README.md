# News Crawler — Hệ thống crawl tin tức tiếng Việt

Hệ thống modular để crawl tin tức từ các báo lớn VN, dùng cho đề tài
**"Hệ thống lưu trữ và phân tích xu hướng tin tức với NoSQL + Time-series DB"**.

Pipeline: **RSS crawler** + **HTML crawler** → merge & dedup → `articles_final.jsonl`.
Tự động hoá daily bằng **GitHub Actions cron** (10h sáng VN).

## 🗂 Cấu trúc

```
crawl_news_v2/
├── crawler_core.py          # Module CHUNG: HTTP, storage, rate limit, dedupe, rotation
├── extractors.py            # Site-specific HTML extractors (JSON-LD + meta + selectors + trafilatura)
├── rss_crawler.py           # RSS crawler (98 feeds) với enrich full content
├── html_crawler.py          # HTML category-page crawler (znews, laodong)
├── build_dataset.py         # Merge html + rss → articles_final.jsonl (dedup, merge keywords)
├── run_all.py               # Entry point — chạy toàn bộ pipeline
├── migrate_csv.py           # Chuyển CSV cũ → JSONL mới
├── requirements.txt
├── .github/workflows/
│   └── crawl.yml            # GitHub Actions cron daily 10h VN
└── data/                    # Output (tự tạo, commit ngược vào repo)
    ├── articles_final.jsonl         # ← file cuối cùng để dùng
    ├── html_articles.jsonl          # raw HTML crawl
    ├── rss_articles.jsonl           # raw RSS crawl
    ├── *_2026-04.jsonl              # partition khi file > 90MB (rotation tự động)
    ├── checkpoint_html.json
    ├── checkpoint_rss.json
    └── *.log                        # log file (không commit)
```

## 🚀 Cài đặt

```bash
pip install -r requirements.txt
```

## 📋 Cách dùng

### 1. Chạy full pipeline (default: lấy bài 7 ngày gần nhất)

```bash
python run_all.py
```

### 2. Chỉ định ngày bắt đầu

```bash
python run_all.py --start 2026-01-14
```

### 3. Chỉ RSS / chỉ HTML

```bash
python run_all.py --rss-only
python run_all.py --html-only
```

### 4. Tuỳ chọn khác

```bash
python run_all.py --no-enrich       # RSS không fetch full content (nhanh hơn)
python run_all.py --skip-build      # Không chạy build_dataset cuối pipeline
python run_all.py --max-pages 30    # Giới hạn số trang/category cho HTML
```

### 5. Build dataset thủ công (không crawl)

```bash
python build_dataset.py             # Gộp html/rss JSONL có sẵn → articles_final.jsonl
```

### 6. Migrate dữ liệu CSV cũ sang JSONL

```bash
python migrate_csv.py \
  --csv /path/to/24h_html_categories_vi.csv:html \
  --csv /path/to/znews_html_categories_vi.csv:html \
  --csv /path/to/rss_feed_articles_v2.csv:rss \
  --output data/migrated_articles.jsonl
```

### 7. Resume sau khi crash
Crawler tự resume — chỉ cần chạy lại cùng lệnh. Checkpoint trong `data/checkpoint_*.json`, dedup dựa vào `seen_ids` load từ **tất cả** partition JSONL (cả file hiện tại + các partition đã rotate).

## 🔄 Cơ chế rotation (tự động partition theo tháng)

Khi 1 file JSONL vượt **90MB**, `JsonlStore` tự rename nó thành `{basename}_{YYYY-MM}.jsonl` (nếu trùng tháng → thêm counter `_2`, `_3`…) rồi tạo file mới trống để tiếp tục ghi. Ngưỡng 90MB chừa buffer an toàn dưới giới hạn 100MB của GitHub.

Dedup không bị mất: `seen_ids` load union từ **tất cả** partition. Cùng 1 URL → cùng `stable_id = md5(normalize_url(url))`, đã crawl 1 lần sẽ không bao giờ ghi lần 2.

Thay đổi ngưỡng: `ROTATE_THRESHOLD_BYTES` trong `crawler_core.py`.

## 🤖 Tự động daily bằng GitHub Actions

Workflow `.github/workflows/crawl.yml`:

- Chạy **03:00 UTC mỗi ngày** = 10h sáng VN (có thể trễ 5-60 phút do best-effort của GitHub cron)
- Cũng chạy được manual qua tab **Actions → Daily News Crawl → Run workflow**
- Sau khi crawl xong, commit ngược `data/*.jsonl` + `data/*.json` vào repo

**Setup lần đầu:**

```bash
git init && git branch -M main
git add . && git commit -m "init: news crawler"
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

Trên GitHub: **Settings → Actions → General → Workflow permissions** → chọn **Read and write permissions** (để bot commit ngược được).

## 📝 Schema output (mỗi dòng JSONL)

```json
{
  "id": "a941805bd131e05bae9a55c1a15d7f62",
  "url": "https://vnexpress.net/...",
  "title": "...",
  "content": "...",
  "published_at": "2026-02-03T13:45:00+00:00",
  "crawled_at": "2026-04-18T07:30:00+00:00",
  "source": "vnexpress",
  "source_domain": "vnexpress.net",
  "source_type": "rss",
  "category_raw": "Thế giới",
  "category_normalized": "world",
  "author": "Đức Hoàng",
  "language": "vi",
  "keywords": ["Starlink", "Ukraine", "Nga"],
  "entities": [],
  "content_length": 3218,
  "has_full_content": true
}
```

## ⚙️ Cấu hình ngưỡng lọc

Trong `crawler_core.py`:

- `MIN_CONTENT_LENGTH_STRICT = 300` — HTML crawler, bài dưới 300 chars bị đánh `content_too_short`
- `MIN_CONTENT_LENGTH_LOOSE = 100` — RSS + build_dataset (RSS summary vốn ngắn)
- `ROTATE_THRESHOLD_BYTES = 90 MB` — ngưỡng rotation file

## 🎯 Các cải tiến so với crawler cũ

| Vấn đề cũ | Giải pháp mới |
|---|---|
| Path CSV có dấu `/` | JsonlStore sanitize filename |
| `source.name` nhầm tên author | Tách 2 field `source` (từ domain) + `author` |
| Content có boilerplate, `&amp;`/`&quot;` | `clean_content()` strip + `html.unescape()` 2 lớp |
| Miss content do selector generic | Site-specific selectors + trafilatura fallback |
| Code duplicate 4 lần | Module chung `crawler_core.py` |
| Không resume sau crash | Checkpoint + JsonlStore load seen_ids |
| Rate limit global → dồn 1 báo | `PerDomainRateLimiter` |
| Không có JSON-LD extractor | `extract_jsonld()` — nguồn sạch nhất |
| File phình to → đụng giới hạn GitHub | Rotation tự động theo tháng khi > 90MB |
| Cross-source dedup thủ công | `build_dataset.py` merge theo `id`, keep content dài hơn |

## 🧪 Test nhanh

```bash
# Test 1 feed RSS
python -c "
from rss_crawler import crawl_feed
from crawler_core import HttpClient, JsonlStore
http = HttpClient()
store = JsonlStore('data/test.jsonl')
stats = crawl_feed('https://vnexpress.net/rss/thoi-su.rss', http, store, None, enrich=True)
print(stats)
"
```

## 📊 Ước tính thời gian

| Scenario | Thời gian ước tính |
|---|---|
| RSS no-enrich | 5-10 phút |
| RSS + enrich full content | 2-4 giờ |
| HTML (znews + laodong) | 1-2 giờ |
| **Full daily run** (7 ngày back) | **20-40 phút** (đa số bài đã dedup) |

## 🔜 Bước tiếp theo (ngoài phạm vi crawler)

1. **NER**: chạy `underthesea.ner()` lên field `content` → populate `entities`
2. **Dedup cross-source**: MinHash/SimHash để gộp bài cùng sự kiện từ nhiều báo
3. **Index vào Elasticsearch + ClickHouse**: bulk import JSONL
4. **Generate synthetic data** cho benchmark (blow up lên 1M-10M events)
