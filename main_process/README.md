# News Analytics Stack — Elasticsearch + ClickHouse

Hạ tầng hoàn chỉnh cho đề tài **"Hệ thống lưu trữ và phân tích xu hướng tin tức với NoSQL + Time-series DB"**.

## 📂 Cấu trúc

```
main_process/
├── docker-compose.yml        # ES + Kibana + ClickHouse stack
├── Makefile                  # 1-command interface cho mọi thao tác
├── es_mapping.json           # ES index mapping với Vietnamese analyzer
├── clickhouse_schema.sql     # ClickHouse schema: 3 bảng + 3 MV
├── 01_clean_entities.py      # Clean noise trong NER output
├── 02_index_to_es.py         # Bulk index vào Elasticsearch
├── 03_load_to_ch.py          # Bulk load vào ClickHouse
└── 04_verify.py              # Demo queries 3 kịch bản
```

Folder flat — không có subdir `scripts/` hay `config/`. Script có thể gọi trực tiếp bằng `python 01_clean_entities.py ...` hoặc qua `make`.

## 🧩 Input / Output (multi-partition)

Pipeline dùng chung helper `pre_dataset/partition_io.py` — hỗ trợ **multi-partition** với rotation 89MB:
- `../data/articles_ner_1.jsonl`, `..._2.jsonl`, ... (output từ NER)
- `../data/articles_ready_1.jsonl`, `..._2.jsonl`, ... (output sau clean_entities)

Glob default của từng script:
| Script | Input glob | Output |
|---|---|---|
| `01_clean_entities.py` | `../data/articles_ner*.jsonl` | `../data/articles_ready.jsonl` (auto-rotate) |
| `02_index_to_es.py`    | `../data/articles_ready*.jsonl` | Elasticsearch index `news_articles` |
| `03_load_to_ch.py`     | `../data/articles_ready*.jsonl` | ClickHouse `news.*` tables |

Có thể override với `--input '<pattern>'` (repeatable). Nhớ quote glob trong bash.

## 🚀 Quick start (TL;DR)

```bash
# 1. Đảm bảo có NER output (chạy pre_dataset/02_ner.py trước)
ls ../data/articles_ner*.jsonl

# 2. Cài dependencies
make install

# 3. Khởi động Docker
make up
make install-icu      # Cài plugin phân tích tiếng Việt (ICU)

# 4. Chạy full pipeline
make all

# Hoặc từng bước:
make clean-entities   # Clean noise từ NER → articles_ready*.jsonl
make index-es         # Index vào ES
make load-ch          # Load vào ClickHouse
make verify           # Demo query 3 kịch bản
```

Sau khi chạy xong, mở:
- **Kibana**: http://localhost:5601 — GUI để browse ES
- **ClickHouse**: `docker exec -it news-ch clickhouse-client`

## 📋 Các bước chi tiết

### Bước A — Khởi động hạ tầng

```bash
make up
```

Start 3 container:
- `news-es`       — Elasticsearch 8.15 (port 9200)
- `news-kibana`   — Kibana (port 5601)
- `news-ch`       — ClickHouse (port 8123 HTTP, 9000 native)

Check status: `make ps`

### Bước B — Cài ICU plugin cho ES

```bash
make install-icu
```

ICU plugin là bắt buộc cho tiếng Việt để tokenize đúng (ES standard analyzer không xử lý tốt). Script này install plugin và restart ES.

Nếu không cài được (offline), script `02_index_to_es.py` sẽ tự fallback về standard analyzer.

### Bước C — Clean noise entities

```bash
make clean-entities
# → output: ../data/articles_ready*.jsonl (auto-rotate 89MB)
```

Loại các entity nhiễu từ NER:
- **Date-like**: "năm 2026", "tháng 3" → remove
- **Pure numeric**: "113 999", "9.5" → remove
- **Percent/currency**: "9,2 %", "100 USD" → remove
- **Common words**: "Bộ", "Điểm", "Người" → remove
- **Too long** (>50 chars): thường do tokenizer gom nhầm → remove
- **Admin prefix**: "xã Sơn Cẩm" → "Sơn Cẩm" + force type=LOC
- **Title prefix**: "ông Trump" → "Trump" + force type=PER

Clean không incremental — full rebuild mỗi lần (chỉ vài phút cho 100k+ bài).

### Bước D — Index vào Elasticsearch

```bash
make index-es
```

- Tạo index `news_articles` với mapping tối ưu (ICU analyzer, nested entities)
- Bulk insert tất cả docs (batch 500) từ mọi partition
- Tạo alias `news_public` cho Kịch bản 3 RBAC (filter: chỉ `is_canonical=true`)

### Bước E — Load vào ClickHouse

```bash
make load-ch
```

Tạo & load 3 bảng chính + 3 materialized view:

| Table | Purpose |
|---|---|
| `news.articles` | 1 row/article — metadata + full content |
| `news.keyword_events` | 1 row per (article × keyword) — long format cho trending |
| `news.entity_events` | 1 row per (article × entity) — cho entity analytics |
| `news.daily_keyword_stats` (MV) | Auto-aggregate theo ngày cho query trending cực nhanh |
| `news.daily_entity_stats` (MV) | Tương tự cho entity |
| `news.hourly_keyword_stats` (MV) | Granularity theo giờ |

### Bước F — Verify

```bash
make verify
```

Chạy demo queries theo 3 kịch bản đề cương:

**KB1 — Search (Elasticsearch):**
- Full-text search với highlight
- Entity-based search (tin có LOC='Iran')
- Combined query (text + entity)

**KB2 — Trending (ClickHouse):**
- Top keywords theo ngày/tuần/tháng, filter theo category
- Hot keywords (spike detection: tuần này vs tuần trước)
- Phân bố bài viết theo giờ trong ngày
- Top PER/LOC/ORG entities
- Cross-source coverage (sự kiện được nhiều báo đưa tin)

**KB3 — RBAC:**
- Admin query `news_articles` → thấy tất cả docs + author
- Guest query `news_public` alias → chỉ canonical + source_excludes author

## 🔧 Schema chi tiết

### Elasticsearch mapping highlights

- `title`, `content`: `text` + ICU analyzer — tìm kiếm tiếng Việt chính xác
- `entities`: **nested field** — query được "bài nhắc Iran (LOC) VÀ Trump (PER)"
- `keywords`, `category_normalized`, `source`: `keyword` → aggregation fast
- `title.raw`: sub-field keyword để sort/aggregate theo title

### ClickHouse design highlights

**Partition by month** (`toYYYYMM(publish_date)`): query theo date range chỉ scan partition liên quan.

**Order by (publish_date, keyword, ...)**: để query top-N keyword theo ngày cực nhanh do sort key.

**LowCardinality(String)** cho `source`, `category_normalized`: memory nhẹ + query nhanh hơn String thường.

**SummingMergeTree** cho stats: ClickHouse tự cộng `mention_count` khi merge → không cần GROUP BY khi query.

## 🛠 Troubleshooting

**ES start chậm / unhealthy:**
```bash
make logs-es       # check log
docker stats news-es   # check memory
```
Giảm heap nếu thiếu RAM: edit `docker-compose.yml` → `ES_JAVA_OPTS=-Xms512m -Xmx512m`.

**ClickHouse connection refused:**
```bash
make logs-ch
# Check: "Ready for connections"
```

**ICU plugin install fail (offline):**
Script `02_index_to_es.py` auto-fallback về standard analyzer. Search vẫn hoạt động nhưng kém chính xác hơn với tiếng Việt.

**Bulk index error "field not in mapping":**
Check `ALLOWED_FIELDS` trong `02_index_to_es.py` — khớp với `es_mapping.json`.

**Glob không match file nào:**
Nhớ quote pattern trong bash: `--input '../data/articles_ready*.jsonl'` (không quote shell expand sớm, có thể sai).
