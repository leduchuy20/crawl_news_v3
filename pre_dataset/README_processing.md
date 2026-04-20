# Processing Pipeline — Bước 1, 2, 3

Xử lý post-crawl: **cleanup** + **NER** + **analyze** cho dataset JSONL multi-partition.

Cả 3 script đều hỗ trợ:

- **Multi-partition input** qua glob (`data/*.jsonl`, `data/migrated_*.jsonl`, …)
- **Output auto-rotation ở 89MB** (an toàn dưới giới hạn 100MB của GitHub)
- Chạy với default args, không cần gõ path dài

## 📂 Files

```text
pre_dataset/
├── partition_io.py   # Helper chung: glob input, JsonlWriter rotation, resume scan
├── 01_cleanup.py     # Bước 1: FULL REBUILD — fix author, expand category, dedup, derived fields
├── 02_ner.py         # Bước 2: INCREMENTAL — NER với underthesea, skip ID đã xong
├── 03_analyze.py     # Bước 3: Verify chất lượng dataset
├── requirements.txt
└── README_processing.md
```

## 🔁 Khi nào chạy cái gì

| Tình huống                            | Chạy                                                         |
| ------------------------------------- | ------------------------------------------------------------ |
| Lần đầu sau khi crawl + migrate xong  | `01` → `02` → `03`                                           |
| Có crawl mới đổ về (daily cron)       | `01` → `02` → `03` (01 rebuild nhanh, 02 chỉ NER bài mới)    |
| Chỉ muốn kiểm tra chất lượng          | `03`                                                         |

Chi tiết từng bước:

- **`01_cleanup` = FULL REBUILD**: cần dedup toàn bộ dataset nên xoá output cũ, đọc lại hết. Fast: ~1–2 phút/150k bài.
- **`02_ner` = INCREMENTAL**: scan ID đã có trong `articles_ner*.jsonl`, chỉ NER phần còn lại. Bài cũ không chạy lại.
- **`03_analyze` = read-only**: không ghi file, chỉ in thống kê.

## 🚀 Cách chạy

### 1. Setup (1 lần)

```bash
pip install underthesea
```

Lần đầu chạy `underthesea`, nó tự download model ~100MB.

### 2. Bước 1 — Cleanup (~1–2 phút cho 150k bài)

```bash
# Default: đọc data/articles_final*.jsonl + data/migrated_*.jsonl
#          ghi data/articles_cleaned.jsonl (auto rotate)
python pre_dataset/01_cleanup.py

# Hoặc explicit
python pre_dataset/01_cleanup.py \
    --input 'data/articles_final*.jsonl' \
    --input 'data/migrated_*.jsonl' \
    --output data/articles_cleaned.jsonl \
    --min-content-len 200
```

**Làm gì:**
- Loại bài content < 200 chars (rác)
- Fix `author`: author == source → clear
- Mở rộng `CATEGORY_MAP` (giảm `other` đáng kể)
- Dedup cross-source bằng normalized title hash → gắn `dup_group_id`, `is_canonical`, `dup_count`
- Thêm field cho ClickHouse: `publish_date`, `publish_hour`, `publish_dow`, `word_count`
- Sort theo `published_at` ASC trước khi ghi
- Xoá mọi `articles_cleaned*.jsonl` cũ và ghi mới với rotation ở 89MB

### 3. Bước 2 — NER (~2-3h cho 12k bài, 4 workers)

```bash
# Single worker (chậm)
python pre_dataset/02_ner.py

# Multi-worker (nhanh 3-4x)
python pre_dataset/02_ner.py --workers 4

# Test nhanh (5-10 phút) — chỉ NER trên title
python pre_dataset/02_ner.py --title-only
```

**Resume tự động:**
Khi crawl mới về + rerun pipeline, `02_ner` scan mọi `articles_ner*.jsonl` để lấy danh sách ID đã có entities, chỉ chạy NER cho phần CHƯA có. Crash giữa chừng cũng chạy lại được — không bao giờ làm lại công việc cũ.

Muốn chạy lại từ đầu: xoá thủ công `rm data/articles_ner*.jsonl`.

### 4. Bước 3 — Analyze

```bash
# Default: đọc data/articles_ner*.jsonl
python pre_dataset/03_analyze.py

# Check intermediate cleanup output
python pre_dataset/03_analyze.py --input 'data/articles_cleaned*.jsonl'
```

In ra: tổng records, phân bố source/category, content length percentiles, duplicate groups, top PER/LOC/ORG entities, time range.

## 📊 Schema sau khi chạy đủ 2 bước

```json
{
  "id": "...",
  "url": "...",
  "title": "...",
  "content": "...",
  "published_at": "2026-02-03T13:45:00+00:00",
  "crawled_at": "...",
  "source": "vnexpress",
  "source_domain": "vnexpress.net",
  "source_type": "rss",
  "category_raw": "Thế giới",
  "category_normalized": "world",
  "author": "Đức Hoàng",
  "language": "vi",
  "keywords": ["..."],
  "entities": [                              ← POPULATED bởi NER
    {"text": "TP.HCM", "type": "LOC"},
    {"text": "Nguyễn Văn Yên", "type": "PER"},
    {"text": "Công an TP.HCM", "type": "ORG"}
  ],
  "content_length": 3218,
  "has_full_content": true,
  "dup_group_id": "a3f2b8c9",                ← dedup cross-source
  "is_canonical": true,
  "dup_count": 2,
  "publish_date": "2026-02-03",              ← cho ClickHouse
  "publish_hour": 20,
  "publish_dow": 1,
  "word_count": 542
}
```

## 📁 Output layout (sau pipeline)

```text
data/
├── articles_cleaned.jsonl              # current (chưa đầy 89MB)
├── articles_cleaned_2026-04.jsonl      # rotated partitions
├── articles_cleaned_2026-04_2.jsonl
├── articles_ner.jsonl                  # current
├── articles_ner_2026-04.jsonl          # rotated
└── ...
```

Naming pattern: `{base}.jsonl` (current) + `{base}_{YYYY-MM}[_N].jsonl` (rotated).

## ⚠️ Lưu ý

- **Bước 2 mất nhiều thời gian nhất.** NER trên 150k bài × content ~3k chars là chậm. Chạy qua đêm với `--workers 4`.
- **Content dài > 5000 chars bị truncate** khi NER (entities từ title + 5000 chars đầu đã đủ cho 95% use case).
- **Memory**: underthesea nạp model ~500MB/process. Với 4 workers cần ~2GB RAM.
- **Full rebuild bước 1 là chủ ý**: dedup title-hash cần toàn bộ dataset trong RAM cùng lúc. Nếu dataset > 1M bài sẽ cần đổi chiến lược.

## 🔜 Bước tiếp theo (ngoài phạm vi pre_dataset)

Sau khi có `data/articles_ner*.jsonl`:

- **Docker Compose**: Elasticsearch + ClickHouse + Kibana
- **Index loader**: đổ vào ES + ClickHouse
- **FastAPI backend** cho kịch bản demo
