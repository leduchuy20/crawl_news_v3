# PINELINE

File này hướng dẫn Claude Code khi làm việc trong repo `crawl_news_v2`.

## Bối cảnh dự án

Hệ thống crawl + phân tích tin tức tiếng Việt cho đề tài
**"Hệ thống lưu trữ và phân tích xu hướng tin tức với NoSQL + Time-series DB"**.

Pipeline 4 stage, các stage chạy độc lập, output stage trước là input stage sau:

```
Stage 1: Crawl + NER  (root, không Docker)        → data/articles_ner*.jsonl
Stage 2: Index + Analytics  (main_process/, Docker) → ES + ClickHouse
Stage 3: Backend API + Frontend  (kb3/)             → http://localhost:8000 + :8080
Stage 4: Benchmark (optional)  (benchmark/)         → ES vs CH vs PG charts
```

Toàn bộ pipeline + commands có sẵn ở `PIPELINE.md` (root). README riêng cho từng stage:
`README.md`, `main_process/README.md`, `kb3/README.md`, `benchmark/README.md`,
`pre_dataset/README_processing.md`.

## Cấu trúc thư mục (top-level)

```
crawl_news_v2/
├── crawler_core.py        # HTTP, JsonlStore rotation, rate limit, dedupe (680 LOC)
├── extractors.py          # Site-specific HTML extractors + JSON-LD + trafilatura
├── rss_crawler.py         # 98 RSS feeds + enrich full content
├── html_crawler.py        # HTML category-page crawler (znews, laodong)
├── build_dataset.py       # Merge html + rss → articles_final.jsonl (dedup)
├── run_all.py             # Entry point Stage 1 — crawl + cleanup + NER
├── migrate_csv.py         # Migrate CSV cũ → JSONL mới
├── requirements.txt       # Deps crawler (requests, feedparser, bs4, trafilatura, underthesea)
├── .github/workflows/crawl.yml   # Cron daily 03:00 UTC (10h sáng VN)
├── pre_dataset/           # Post-crawl: cleanup + NER + analyze
├── main_process/          # Stage 2: ES + Kibana + ClickHouse, scripts 01–04
├── kb3/                   # Stage 3: FastAPI backend + SPA frontend
├── benchmark/             # Stage 4: PostgreSQL + benchmark scripts
└── data/                  # Output JSONL (multi-partition, commit ngược qua GHA)
```

## Quy ước data quan trọng

### Multi-partition JSONL với rotation 89/90MB

Tất cả file output là JSONL — 1 dòng = 1 JSON object. Khi file > **90MB** (root crawler)
hoặc **89MB** (pre_dataset/main_process), `JsonlStore` / `partition_io.JsonlWriter` tự
rename thành `{basename}_{YYYY-MM}.jsonl` (trùng tháng → counter `_2`, `_3`…) rồi mở
file mới. Ngưỡng này nằm dưới giới hạn 100MB của GitHub.

Hệ quả: **bất cứ khi nào đọc/ghi dataset, phải dùng glob** (`articles_ner*.jsonl`),
không hardcode một file. Helper chung ở `pre_dataset/partition_io.py`.

### Pipeline file dòng chảy

```
rss_articles.jsonl    ┐
html_articles.jsonl   ┼─ build_dataset.py ─→ articles_final*.jsonl
                      │                       │
                      │                       ▼
                      │              pre_dataset/01_cleanup.py
                      │                       │
                      │                       ▼
                      │                articles_cleaned*.jsonl
                      │                       │
                      │                       ▼
                      │              pre_dataset/02_ner.py   (INCREMENTAL, skip ID đã NER)
                      │                       │
                      │                       ▼
                      │                articles_ner*.jsonl  ← input main_process/
                      │                       │
                      ▼                       ▼
              checkpoint_*.json   main_process/01_clean_entities.py
                                              │
                                              ▼
                                     articles_ready*.jsonl  ← input ES + CH + benchmark
```

### Dedup theo `stable_id`

`id = md5(normalize_url(url))`. `JsonlStore` load `seen_ids` từ **TẤT CẢ** partition khi
khởi động → resume sau crash không bao giờ ghi trùng. `build_dataset.py` merge cross-source
theo `id`, giữ content dài hơn. `pre_dataset/01_cleanup.py` thêm `dup_group_id` /
`is_canonical` / `dup_count` dedup theo normalized-title-hash (gộp bài cùng sự kiện từ
nhiều báo).

### Schema cuối cùng (sau Stage 1)

```json
{
  "id": "a941805bd131e05bae9a55c1a15d7f62",
  "url": "...", "title": "...", "content": "...",
  "published_at": "2026-02-03T13:45:00+00:00",
  "crawled_at": "...",
  "source": "vnexpress", "source_domain": "vnexpress.net",
  "source_type": "rss",
  "category_raw": "Thế giới", "category_normalized": "world",
  "author": "...", "language": "vi",
  "keywords": ["..."], "entities": [{"text": "...", "type": "PER|LOC|ORG"}],
  "content_length": 3218, "has_full_content": true,
  "dup_group_id": "...", "is_canonical": true, "dup_count": 2,
  "publish_date": "2026-02-03", "publish_hour": 20, "publish_dow": 1,
  "word_count": 542
}
```

## Lệnh hay dùng

### Stage 1 — Crawl + NER (root, Python only)

```bash
pip install -r requirements.txt

python run_all.py                        # full pipeline, 7 ngày back, ~20-40 phút
python run_all.py --rss-only             # chỉ RSS
python run_all.py --html-only            # chỉ HTML
python run_all.py --start 2026-01-14     # từ ngày cụ thể
python run_all.py --no-enrich            # RSS không fetch full (nhanh)
python run_all.py --skip-cleanup --skip-ner  # bỏ post-process
python run_all.py --ner-workers 4        # local: 4 workers, GHA: để default 2

# Chạy lẻ từng bước
python rss_crawler.py
python html_crawler.py
python build_dataset.py
python pre_dataset/01_cleanup.py    # FULL REBUILD, ~1-2 phút
python pre_dataset/02_ner.py --workers 4    # INCREMENTAL, scan ID đã có
python pre_dataset/03_analyze.py    # in stats, không ghi
```

### Stage 2 — Index + Analytics (`main_process/`, Docker)

```bash
cd main_process
make install          # pip elasticsearch + clickhouse-driver
make up               # docker-compose up: news-es, news-kibana, news-ch
make install-icu      # ICU plugin cho tiếng Việt (BẮT BUỘC nếu online)
make all              # clean-entities → index-es → load-ch → verify

# Hoặc từng bước
python 01_clean_entities.py          # NER → ready, FULL REBUILD
python 02_index_to_es.py --reset     # ready → ES (alias news_public cho RBAC)
python 03_load_to_ch.py --reset      # ready → CH (3 tables + 3 MV)
python 04_verify.py                  # CLI demo 3 kịch bản

make down             # giữ data
make clean-docker     # XOÁ volume → mất ES index + CH table
```

`--reset` = drop + recreate. Không flag = append. Endpoint:
- ES: <http://localhost:9200>, Kibana: <http://localhost:5601>
- ClickHouse: HTTP `:8123`, native `:9000`, CLI `docker exec -it news-ch clickhouse-client`

### Stage 3 — Backend + Frontend (`kb3/`)

Yêu cầu Stage 2 Docker đang up + có data. 2 terminal:

```bash
# Terminal 1 (PowerShell)
cd kb3
make install
$env:PYTHONPATH="."
uvicorn backend.main:app --reload --port 8000
# Hoặc bash: PYTHONPATH=. uvicorn ...

# Terminal 2
cd kb3/frontend 
python -m http.server 8080
```

URL: <http://localhost:8080> (UI), <http://localhost:8000/docs> (Swagger).
Demo accounts: `admin/admin123` (full), `guest/guest123` (canonical only, ẩn author+url).

### Stage 4 — Benchmark ES vs CH vs PG (`benchmark/`, optional)

```bash
cd benchmark
make install          # psycopg2-binary, elasticsearch, clickhouse-driver, matplotlib
make up               # news-pg container (cùng network main_process_news-net)
python scripts/01_load_pg.py --reset                 # load + GIN/tsvector index
python scripts/02_run_benchmark.py --runs 20 --warmup 3   # ~15-30 phút
#### PNG
$latest = (Get-ChildItem results\benchmark_*.csv | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
python scripts\03_plot_results.py --csv results\$latest
```

## Quy tắc khi sửa code

### Cảnh giác với multi-partition

- **Đọc dataset**: luôn glob (`glob.glob('data/articles_ner*.jsonl')`) — đừng hardcode 1 file.
- **Ghi dataset**: dùng `crawler_core.JsonlStore` (Stage 1) hoặc
  `pre_dataset.partition_io.JsonlWriter` (Stage 2 onwards) để tự rotation đúng.
- **Dedup**: load `seen_ids` từ TẤT CẢ partition trước khi ghi.

### Ngưỡng filter (đừng đổi bừa)

Trong `crawler_core.py`:
- `MIN_CONTENT_LENGTH_STRICT = 300` — HTML crawler, < 300 chars → `content_too_short`
- `MIN_CONTENT_LENGTH_LOOSE = 100` — RSS + build_dataset (RSS summary ngắn)
- `ROTATE_THRESHOLD_BYTES = 90 MB`

Trong `pre_dataset/01_cleanup.py`:
- `--min-content-len 200` default — loại rác

### Rate limit & retry

Per-domain rate limit ở `crawler_core.PerDomainRateLimiter`, default `min_delay=0.8s`.
HTTP retry 3 lần với exponential backoff, special handling cho 429/503. SSL có
`_build_legacy_ssl_context()` để xử lý báo VN dùng TLS server cũ
(UNSAFE_LEGACY_RENEGOTIATION_DISABLED trên OpenSSL 3.x).

### `01_cleanup.py` là FULL REBUILD, `02_ner.py` là INCREMENTAL

- Cleanup: xoá hết `articles_cleaned*.jsonl` rồi đọc lại toàn bộ + dedup title-hash trong RAM.
  Khi dataset > 1M bài cần đổi chiến lược.
- NER: scan `articles_ner*.jsonl` lấy ID đã NER, chỉ chạy phần còn lại.
  Muốn redo: `rm data/articles_ner*.jsonl`.

### NER underthesea

- Mỗi process load model ~500MB → 4 workers tốn ~2GB RAM.
- Content > 5000 chars bị truncate khi NER (entities từ title + 5000 chars đầu).
- Lần đầu chạy auto-download model ~100MB.

### Schema field mapping

- ES mapping: `main_process/es_mapping.json` — `title`/`content` text + ICU,
  `entities` **nested** (để query "Iran AND Trump"), `keywords`/`category_normalized` keyword.
- ClickHouse schema: `main_process/clickhouse_schema.sql` — `news.articles`,
  `news.keyword_events`, `news.entity_events` + 3 MV (`daily_keyword_stats`,
  `daily_entity_stats`, `hourly_keyword_stats`). Partition by `toYYYYMM(publish_date)`.
- Khi thêm field vào pipeline phải đồng bộ: `01_cleanup.py` → `es_mapping.json` →
  `ALLOWED_FIELDS` trong `02_index_to_es.py` → `clickhouse_schema.sql` → loader 03.

### RBAC ở Stage 3

- Index gốc `news_articles` = admin xem hết.
- Alias `news_public` filter `is_canonical=true`, `source_excludes: ["author"]` =
  guest xem.
- JWT secret + passwords override qua env: `JWT_SECRET`, `ADMIN_PASSWORD`, `GUEST_PASSWORD`.
  Default chỉ dùng dev.

`articles_ner*.jsonl`, `articles_cleaned*.jsonl`, `articles_final*.jsonl` **CÓ** commit
(input cho người dùng tải về dùng tiếp). `articles_ready*.jsonl` thì không (rebuild được
qua Stage 2).
