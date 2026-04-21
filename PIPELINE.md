# Pipeline — Hướng dẫn chạy & tắt

Tài liệu 1 trang: chạy toàn bộ hệ thống từ **crawl tin tức** → **index + phân tích** → **API + giao diện demo**, và tắt gọn khi xong.

---

## Tổng quan — 3 stage

```
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 1 — CRAWL + NER  (root folder, không cần Docker)               │
│                                                                      │
│   run_all.py                                                         │
│     ├─ rss_crawler.py        → data/articles_rss.jsonl               │
│     ├─ html_crawler.py       → data/articles_final.jsonl             │
│     ├─ build_dataset.py      → articles_final (merged)               │
│     ├─ pre_dataset/01_cleanup.py → articles_cleaned*.jsonl           │
│     └─ pre_dataset/02_ner.py     → articles_ner*.jsonl  (NER tag)    │
└──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 2 — INDEX + ANALYTICS  (main_process/, Docker up)              │
│                                                                      │
│   main_process/                                                      │
│     ├─ 01_clean_entities.py  → articles_ready*.jsonl                 │
│     ├─ 02_index_to_es.py     → Elasticsearch (news_articles + alias) │
│     ├─ 03_load_to_ch.py      → ClickHouse (news.articles + 3 MV)     │
│     └─ 04_verify.py          → CLI demo 3 kịch bản                   │
└──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 3 — BACKEND API + FRONTEND DEMO  (kb3/, Docker Stage 2 up)     │
│                                                                      │
│   kb3/                                                               │
│     ├─ backend/                                                      │
│     │   ├─ main.py           → FastAPI http://localhost:8000         │
│     │   ├─ routers/          → /auth, /search, /trends, /admin       │
│     │   └─ deps/             → ES + ClickHouse clients               │
│     └─ frontend/index.html   → SPA http://localhost:8080             │
└──────────────────────────────────────────────────────────────────────┘
```

**Phụ thuộc**: Stage 2 cần Stage 1 đã có `articles_ner*.jsonl`. Stage 3 cần Stage 2 đã có ES index + ClickHouse table.

---

## Stage 1 — Crawl + NER

Chạy ở root `d:\work\crawl_news_v2`.

**Lần đầu** — cài Python deps:
```bash
pip install -r requirements.txt
```

**Chạy full:**
```bash
python run_all.py
```

Hoặc từng bước tách riêng để debug:
```bash
python rss_crawler.py
python html_crawler.py
python build_dataset.py
python pre_dataset/01_cleanup.py
python pre_dataset/02_ner.py --workers 4
```

Output: [data/](data/) có `articles_ner*.jsonl` (multi-partition, tự rotate ~89MB).

---

## Stage 2 — Index + Analytics

Chạy ở `main_process/`. Cần Docker Desktop đang chạy.

### Lần đầu — khởi động Docker + cài plugin

```bash
cd main_process
pip install "elasticsearch>=8,<9" clickhouse-driver
docker-compose up -d
docker exec news-es bin/elasticsearch-plugin install --batch analysis-icu
docker restart news-es
```

Đợi ~15s cho ES khởi động lại, check:
```bash
docker ps                     # phải thấy news-es, news-kibana, news-ch
curl http://localhost:9200    # ES trả JSON info
curl http://localhost:8123/ping    # ClickHouse trả 'Ok.'
```

### Chạy pipeline analytics

```bash
python 01_clean_entities.py          # NER → ready (~vài phút, full rebuild)
python 02_index_to_es.py --reset     # ready → ES
python 03_load_to_ch.py --reset      # ready → CH
python 04_verify.py                  # CLI demo 3 kịch bản
```

### Khi nào cần `--reset`?

| Flag | Tác dụng | Khi nào |
|---|---|---|
| `--reset` | Drop + tạo lại index/schema | Lần đầu, schema đổi, muốn clear data cũ |
| (không flag) | Append thêm, giữ doc cũ | Incremental daily update |

---

## Stage 3 — Backend API + Frontend

Chạy ở `kb3/`. **Yêu cầu**: Stage 2 Docker đang up + ES/CH đã có data.

### Cách A — Local dev (khuyến nghị cho Windows, không cần Docker thêm)

Mở **2 terminal** song song.

**Terminal 1 — Backend:**
```bash
cd kb3
pip install -r backend/requirements.txt
```
Rồi từ thư mục `kb3/`:

```powershell
# PowerShell
$env:PYTHONPATH="."
uvicorn backend.main:app --reload --port 8000
```

Hoặc bash (Git Bash):

```bash
PYTHONPATH=. uvicorn backend.main:app --reload --port 8000
```

Log thành công:

```text
✓ Elasticsearch: http://localhost:9200
✓ ClickHouse 24.8...: localhost:9000
Uvicorn running on http://0.0.0.0:8000
```

**Terminal 2 — Frontend:**
```bash
cd kb3/frontend
python -m http.server 8080
```

### Cách B — Docker (production-like)

```bash
cd kb3
docker-compose build
docker-compose up -d
```

Container `news-api` (backend) + `news-ui` (nginx) sẽ join chung network `main_process_news-net` để truy cập ES/CH.

### Mở trình duyệt

| URL | Mục đích |
|---|---|
| <http://localhost:8080> | Frontend demo (giao diện chính) |
| <http://localhost:8000/docs> | Swagger UI — test API thủ công |
| <http://localhost:8000/health> | Health check JSON |
| <http://localhost:5601> | Kibana (GUI cho ES) |

### Demo accounts

| Username | Password | Role |
|---|---|---|
| `admin` | `admin123` | Full access, thấy cả hidden fields |
| `guest` | `guest123` | Chỉ thấy canonical docs, ẩn `author` + `url` |

Không login → anonymous (quyền như `guest`). Trên UI: click avatar góc phải → Settings drawer → Sign in.

### 3 kịch bản có thể demo trên UI

- **KB1 — Search (ES)**: full-text, entity search (PER/LOC/ORG), filter category, highlight, modal chi tiết bài với entities + keywords
- **KB2 — Trends (ClickHouse)**: top keywords, hot spike (×multiplier), timeline, phân bố giờ, top LOC/PER entities, cross-source coverage
- **KB3 — RBAC**: so sánh stats `news_articles` vs alias `news_public`, so sánh cùng 1 article giữa admin vs guest (side-by-side diff)

---

## Chạy lại sau khi tắt Docker

### Case A — `docker-compose down` (không `-v`) → data còn nguyên

Volume còn → index ES + table CH vẫn có. **Không cần chạy lại 01/02/03.**
```bash
cd main_process
docker-compose up -d
python 04_verify.py          # xác nhận data còn
# Stage 3: khởi động lại backend + frontend (terminal 1 + 2 ở trên)
```

### Case B — `docker-compose down -v` → volume mất, load lại từ đầu

```bash
cd main_process
docker-compose up -d
docker exec news-es bin/elasticsearch-plugin install --batch analysis-icu
docker restart news-es
# 01 có thể skip nếu articles_ready*.jsonl đã có từ lần trước
python 01_clean_entities.py
python 02_index_to_es.py --reset
python 03_load_to_ch.py --reset
python 04_verify.py
```

### Case C — Có data NER mới từ Stage 1

```bash
cd main_process
docker-compose up -d                 # nếu chưa up
python 01_clean_entities.py          # BẮT BUỘC vì có NER mới
python 02_index_to_es.py --reset     # reset để rebuild full index
python 03_load_to_ch.py --reset
python 04_verify.py
```

---

## Kiểm tra kết quả thủ công

### ClickHouse CLI

Chạy ở terminal host (bất kỳ folder nào):
```bash
docker exec -it news-ch clickhouse-client
```
Thử query:
```sql
SELECT keyword, sum(mention_count) AS c
FROM news.daily_keyword_stats
WHERE publish_date >= today() - 7
GROUP BY keyword ORDER BY c DESC LIMIT 20;
```
Thoát: `exit` hoặc `Ctrl+D`.

### Elasticsearch qua Kibana

Mở <http://localhost:5601> → Dev Tools → chạy DSL:

```text
GET news_articles/_count
GET news_public/_count
GET news_articles/_search
{ "query": { "match": { "title": "Iran" } }, "size": 3 }
```

Khi tạo Data View lần đầu trong **Discover**: chọn pattern `news_*`, timestamp field = `published_at`. Nếu thấy "no available fields" → mở rộng time range lên **Last 1 year**.

---

## Cách tắt

### Tắt Stage 3 (backend + frontend)

- Terminal uvicorn: `Ctrl+C`
- Terminal http.server: `Ctrl+C`
- (Nếu chạy Docker ở Stage 3) `cd kb3 && docker-compose down`

### Tắt Stage 2 Docker stack (giữ data)

```bash
cd main_process
docker-compose down
```
Container dừng, **volume còn** → lần sau `up -d` thấy lại index/table cũ.

### Tắt và xoá sạch data (reset hoàn toàn)

```bash
cd main_process
docker-compose down -v
```
Xoá cả volume → ES index + CH table biến mất, phải re-run `02` + `03` với `--reset`.

### Dừng 1 container riêng

```bash
docker stop news-es       # hoặc news-kibana / news-ch / news-api / news-ui
docker start news-es      # start lại
```

### Dừng Docker Desktop hẳn

- System tray → right-click Docker icon → **Quit Docker Desktop**.
- Tiết kiệm RAM. Lần sau mở Docker Desktop → `docker-compose up -d` để restart.

---

## Lệnh Docker hữu ích

```bash
# Status
docker ps                        # đang chạy
docker ps -a                     # tất cả (kể cả stopped)

# Log (follow)
docker logs -f --tail 50 news-es
docker logs -f --tail 50 news-ch
docker logs -f --tail 50 news-api

# Vào shell
docker exec -it news-es bash
docker exec -it news-ch bash

# RAM/CPU
docker stats

# Restart 1 container
docker restart news-es
```

---

## Troubleshooting

| Vấn đề | Giải pháp |
|---|---|
| `curl: (7) Failed to connect to localhost:9200` | ES chưa up xong — đợi 30s, `docker logs news-es` |
| `Cannot connect to the Docker daemon` | Docker Desktop chưa chạy — mở lên |
| `routing doesn't support values of type: VALUE_NULL` | ES 8.x issue — đã fix ở [02_index_to_es.py](main_process/02_index_to_es.py) |
| `Unknown expression 'source'` ở cross-source | MV không có cột `source` — đã fix dùng `news.keyword_events` raw |
| `PartiallyConsumedQueryError: Simultaneous queries` | CH client share connection — đã fix ở [kb3/backend/deps/clients.py](kb3/backend/deps/clients.py) (fresh client / request) |
| `422 Unprocessable Entity` khi gọi `/trends/cross-source?days=90` | Đã fix raise `le=60` → `le=365` |
| `ModuleNotFoundError: backend` | Thiếu `PYTHONPATH=.` — set trước khi chạy uvicorn |
| `no input files matched` ở Stage 2 | Chưa có NER output — chạy Stage 1 trước |
| ES OOM hoặc chậm | Giảm heap ở [main_process/docker-compose.yml](main_process/docker-compose.yml): `ES_JAVA_OPTS=-Xms512m -Xmx512m` |
| Port 8000/8080/9200/5601/9000 bị chiếm | `docker-compose down` stack cũ, hoặc đổi port trong compose |
| Frontend gọi API bị CORS / network error | Mở Settings drawer (avatar góc phải) → sửa **API URL** cho đúng backend |
| 403 khi vào RBAC stats | Chưa login admin → Settings → Sign in bằng `admin/admin123` |

---

## TL;DR — lệnh chạy hằng ngày

```bash
# 1. (optional) Crawl + NER nếu có tin mới
python run_all.py

# 2. Index + analytics
cd main_process
docker-compose up -d
python 01_clean_entities.py          # bỏ qua nếu không có NER mới
python 02_index_to_es.py --reset     # bỏ qua nếu data ES còn
python 03_load_to_ch.py --reset      # bỏ qua nếu data CH còn
python 04_verify.py

# 3. Backend + Frontend — mở 2 terminal ở kb3/
# Terminal 1:
cd kb3 && PYTHONPATH=. uvicorn backend.main:app --reload --port 8000
# Terminal 2:
cd kb3/frontend && python -m http.server 8080

# 4. Mở trình duyệt: http://localhost:8080

# 5. Xong — tắt
#   Ctrl+C ở 2 terminal
cd main_process && docker-compose down
```
