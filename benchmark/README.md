# Benchmark — Elasticsearch vs ClickHouse vs PostgreSQL

So sánh hiệu năng 3 engine trên cùng dataset và cùng query, dùng làm số liệu cho báo cáo đề tài.

## 📂 Files

```
benchmark/
├── docker-compose.yml          # Postgres-only stack
├── config/
│   └── pg_schema.sql           # Postgres schema + tsvector + GIN indexes
├── scripts/
│   ├── 01_load_pg.py           # Load JSONL vào Postgres
│   ├── 02_run_benchmark.py     # Runner chính
│   └── 03_plot_results.py      # Vẽ chart từ CSV
├── results/                    # Output (auto-generated)
│   ├── benchmark_YYYYMMDD.json
│   ├── benchmark_YYYYMMDD.csv
│   ├── benchmark_YYYYMMDD.md
│   ├── chart_fulltext.png
│   ├── chart_aggregation.png
│   ├── chart_entity.png
│   └── chart_speedup.png
├── Makefile
└── README.md
```

## 🧪 Methodology

### Fair comparison principles

1. **Cùng dataset**: dùng chung `articles_ready*.jsonl` load vào cả 3 engine
   (đã clean entities + lọc keyword category — giống y hệt data đã vào ES + ClickHouse)
2. **Cùng hardware**: chạy trên cùng 1 máy/container
3. **Cùng query logic**: query Postgres được viết để trả về kết quả **tương đương** với ES/CH
4. **Indexes đầy đủ phía Postgres**: KHÔNG cripple Postgres — ta tạo đủ tsvector GIN, trigram GIN, JSONB GIN

### Test categories

**1. Full-text search** (4 variants)
- **Elasticsearch** `multi_match` title+content với analyzer — giải pháp chính
- **PG-tsvector** — best-effort FTS với `GIN(fts)` index
- **PG-trigram** — `ILIKE '%x%'` với `GIN(... gin_trgm_ops)` index
- **PG-seqscan** — baseline: `LIKE` không index (forced via `enable_indexscan=off`)

**2. Aggregation / trending** (3 variants)
- **ClickHouse-MV** — query `daily_keyword_stats` (SummingMergeTree materialized view)
- **ClickHouse-raw** — query `keyword_events` trực tiếp (tương đương Postgres)
- **Postgres** — `GROUP BY` trên `keyword_events` với B-tree index `(publish_date, keyword)`

**3. Entity search** (nested)
- **Elasticsearch** nested query
- **Postgres** JSONB `@>` containment với GIN `jsonb_path_ops` index

### Metrics
- Mỗi query chạy **20 lần**, bỏ **3 warmup** đầu (cold cache)
- Report: min, median, p95, max, stdev, throughput (QPS)

## 🚀 Cách chạy

### Prerequisites

1. Stack ES + ClickHouse đã chạy + có data: `cd ../main_process && make all`
2. File `../data/articles_ready*.jsonl` đã có (output của `main_process/01_clean_entities.py` —
   dataset đã clean entities + lọc keyword category, fair với ES/CH)

### Chạy benchmark
```bash
# Cài deps (1 lần)
make install

# Khởi động Postgres container
make up

# Load data vào Postgres (lần đầu — mất 5-10 phút, phần lớn là tạo GIN index)
make load

# Chạy benchmark (20 runs × ~40 queries = ~15-30 phút)
make run

# Vẽ chart PNG từ kết quả mới nhất
make plot
```

Kết quả trong `results/`.

### Chạy nhanh (quick test)
```bash
make quick    # 10 runs thay vì 20 — nhanh hơn 2×
```

### Chọn category
```bash
python scripts/02_run_benchmark.py --skip-fulltext
python scripts/02_run_benchmark.py --skip-aggregation
python scripts/02_run_benchmark.py --skip-entity
```

## 📊 Output

### 3 file per run

**`benchmark_<ts>.json`** — raw numbers cho post-processing
```json
[
  {
    "category": "fulltext",
    "scenario": "Search 'Iran'",
    "engine": "Elasticsearch",
    "median_ms": 12.5,
    "p95_ms": 18.3,
    "qps": 80.0,
    "result_count": 340
  },
  ...
]
```

**`benchmark_<ts>.md`** — ready-to-paste table cho báo cáo
Với mỗi scenario, rank engine theo median và tính speedup:

> ### Search 'Iran'
> | Engine | Query | Median | P95 | QPS | Count |
> |---|---|---:|---:|---:|---:|
> | Elasticsearch | multi_match | 12.5ms **(fastest)** | 18.3ms | 80.0 | 340 |
> | PG-tsvector | fts @@ tsquery | 45.2ms (3.6× slower) | ... |
> | PG-trigram | ILIKE GIN | 128ms (10.2× slower) | ... |
> | PG-seqscan | LIKE no index | 2340ms (187× slower) | ... |

**`benchmark_<ts>.csv`** — cho Excel / pandas

### Charts

**`chart_fulltext.png`** — horizontal bar log-scale so sánh latency 4 engines
**`chart_aggregation.png`** — CH-MV vs CH-raw vs PG cho trending
**`chart_entity.png`** — ES nested vs PG JSONB
**`chart_speedup.png`** — speedup của ES/CH so với PG baseline (log scale)

## 💡 Dự kiến kết quả (trên dataset ~12k bài + 60k events)

| Category | Fastest | Slowest | Expected speedup |
|---|---|---|---|
| Full-text search | ES (~10-30ms) | PG seqscan (1-5s) | **30-200×** |
| Full-text vs PG-tsvector | ES | PG tsvector (50-100ms) | **2-5×** |
| Aggregation (Top N 7d) | CH-MV (~5-15ms) | PG (50-200ms) | **10-20×** |
| Complex aggregation (hot spike) | CH-MV (~20-50ms) | PG (200ms-2s) | **10-50×** |
| Entity nested search | ES (~20-60ms) | PG JSONB (50-200ms) | **2-5×** |

Số chính xác sẽ vary theo:
- **Dataset size** — scale lên 1M+ bài, gap càng lớn (ES/CH scale linear, PG scale superlinear với data)
- **Query complexity** — query càng phức tạp, gap càng lớn
- **Hardware** — SSD vs HDD ảnh hưởng Postgres nhiều hơn ES

## 🎯 Cách dùng cho báo cáo

### Slide "Why not just Postgres?"
1. Dán bảng từ `benchmark_<ts>.md` vào slide
2. Chèn chart `chart_speedup.png` — cho thấy speedup theo thứ tự
3. Kết luận: với workload trending + full-text tiếng Việt, ES+CH nhanh gấp 10-100× Postgres

### Argument trong báo cáo
- Postgres **không hẳn chậm** — tsvector đã có index — nhưng kiến trúc **single-node row-store** không scale tốt cho analytics
- ES được thiết kế CHO full-text → scoring, highlighting, Vietnamese analyzer ngoài tốc độ
- ClickHouse (columnar) scan hàng triệu row chỉ bằng vài ms — partition + LowCardinality encoding

## 🔧 Tuning notes

### Postgres đã được tune (trong docker-compose)
- `shared_buffers=512MB`
- `work_mem=64MB`
- `effective_cache_size=1GB`
- `random_page_cost=1.1` (assume SSD)

Nếu muốn benchmark với default config → bỏ các flag trong compose.

### Fairness với Vietnamese text
Postgres `to_tsvector('simple', ...)` + `unaccent` đã approximately tương đương ES ICU analyzer cho tiếng Việt. Cả 2 đều **không phải** tokenizer tiếng Việt chuyên nghiệp, nhưng **fair với nhau**.

Nếu muốn tăng độ chính xác Postgres, có thể thử `pg_bigm` hoặc `pgroonga`, nhưng complexity tăng nhiều.

## 🐛 Troubleshooting

**Postgres connect refused:**
```bash
docker logs news-pg
# Check "database system is ready to accept connections"
```

**"relation articles does not exist":**
Chạy `make load` trước `make run`.

**Benchmark quá chậm (>1 phút/query):**
- Giảm `--runs` hoặc bỏ `--warmup`
- Check resources: `docker stats`
- Postgres có thể cần `VACUUM ANALYZE` (load script tự gọi, nhưng sau test load lần 2 nên chạy lại)

**Chart trông xấu:**
- Thêm `--no-log` nếu không muốn log scale
- Edit `ENGINE_COLORS` trong `03_plot_results.py` để đổi màu
