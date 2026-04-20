# 🚀 Pipeline — Hướng dẫn chạy & tắt

Tài liệu 1 trang: chạy toàn bộ pipeline từ crawl → phân tích, và tắt gọn khi xong.

---

## 📐 Tổng quan pipeline

```
┌────────────────────────────────────────────────────────────────┐
│ STAGE 1 — CRAWL + PROCESS (root folder, không cần Docker)      │
│                                                                │
│ run_all.py                                                     │
│   ├─ rss_crawler.py  ─┐                                        │
│   ├─ html_crawler.py ─┼→ build_dataset.py → data/articles_final│
│   ├─ pre_dataset/01_cleanup.py  → articles_cleaned             │
│   └─ pre_dataset/02_ner.py      → articles_ner (NER tag)       │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ STAGE 2 — INDEX + ANALYTICS (main_process/, cần Docker up)     │
│                                                                │
│ 01_clean_entities.py → articles_ready*.jsonl                   │
│ 02_index_to_es.py    → Elasticsearch (news_articles + alias)   │
│ 03_load_to_ch.py     → ClickHouse (news.articles + MV)         │
│ 04_verify.py         → demo 3 kịch bản                         │
└────────────────────────────────────────────────────────────────┘
```

---

## ▶️ CÁCH CHẠY

### Stage 1 — Crawl + NER (chạy ở root `d:\work\crawl_news_v2`)

**Lần đầu** — cài deps:
```bash
pip install -r requirements.txt
```

**Chạy full pipeline crawl + NER:**
```bash
python run_all.py
```

Hoặc chạy từng bước tách riêng (để debug):
```bash
python rss_crawler.py
python html_crawler.py
python build_dataset.py
python pre_dataset/01_cleanup.py
python pre_dataset/02_ner.py --workers 4
```

Sau bước này, [data/](data/) có `articles_ner*.jsonl` (multi-partition).

---

### Stage 2 — Index + Analytics (chạy ở `main_process/`)

**Lần đầu** — cài deps + khởi động Docker:
```bash
cd main_process
pip install 'elasticsearch>=8,<9' clickhouse-driver
docker-compose up -d
docker exec news-es bin/elasticsearch-plugin install --batch analysis-icu
docker restart news-es
```

Đợi ~15s cho ES khởi động lại, check:
```bash
docker ps                        # 3 container: news-es, news-kibana, news-ch
curl http://localhost:9200       # ES trả về JSON info
```

**Chạy pipeline analytics:**
```bash
python 01_clean_entities.py          # ner → ready (~vài phút, full rebuild)
python 02_index_to_es.py --reset     # ready → Elasticsearch
python 03_load_to_ch.py --reset      # ready → ClickHouse
python 04_verify.py                  # demo 3 kịch bản
```

---

## 🔄 CHẠY LẠI SAU KHI TẮT DOCKER

**Tuỳ cách tắt lần trước** — có 2 kịch bản khác nhau:

### Case A — Tắt bằng `docker-compose down` (không `-v`) → data còn nguyên

Chỉ cần up stack lại + verify. **Không chạy lại `01/02/03`** vì ES index + ClickHouse table vẫn có:

```bash
cd main_process
docker-compose up -d
python 04_verify.py     # đảm bảo data còn nguyên
```

### Case B — Tắt bằng `docker-compose down -v` → volume mất, phải load lại từ đầu

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
##python 04_verify.py --search-query "Trump" --entity-query "Mỹ"
```

### Case C — Có data NER mới (vừa chạy lại crawl + NER ở stage 1)

```bash
cd main_process
docker-compose up -d                 # nếu Docker chưa up
python 01_clean_entities.py          # BẮT BUỘC vì có NER mới
python 02_index_to_es.py --reset     # reset = rebuild full index
python 03_load_to_ch.py --reset
python 04_verify.py
```

### 💡 Khi nào dùng `--reset`?

| Flag | Tác dụng | Khi nào |
|---|---|---|
| `--reset` | Drop rồi tạo lại index/schema | Lần đầu load, hoặc schema đổi, hoặc muốn clear data cũ |
| (không flag) | Append thêm, giữ doc cũ | Incremental update daily (thêm bài mới, không đụng bài cũ) |

---

## 🔎 KIỂM TRA KẾT QUẢ

**Kibana (GUI cho ES):**
- Mở trình duyệt: http://localhost:5601
- Vào Dev Tools chạy query DSL

**ClickHouse CLI** (chạy ở terminal host, bất kỳ folder nào):
```bash
docker exec -it news-ch clickhouse-client
```
Rồi thử:
```sql
SELECT keyword, sum(mention_count) AS c
FROM news.daily_keyword_stats
WHERE publish_date >= today() - 7
GROUP BY keyword ORDER BY c DESC LIMIT 20;
```
Thoát: `exit` hoặc `Ctrl+D`.

---

## ⏹ CÁCH TẮT

### Tắt Docker stack (giữ data)
```bash
cd main_process
docker-compose down
```
→ Dừng + xoá container. **Data trong volume vẫn còn** — lần sau `up -d` sẽ thấy lại index/table cũ.

### Tắt và xoá sạch data (reset hoàn toàn)
```bash
cd main_process
docker-compose down -v
```
→ Xoá cả container + volume. ES index và ClickHouse table biến mất, phải `02_index_to_es.py --reset` + `03_load_to_ch.py --reset` lại từ đầu.

### Dừng 1 container cụ thể (không down cả stack)
```bash
docker stop news-es       # dừng Elasticsearch
docker stop news-kibana   # dừng Kibana
docker stop news-ch       # dừng ClickHouse
```
Start lại: `docker start <tên>`.

### Dừng Docker Desktop hẳn
- Mở Docker Desktop → Quit (hoặc right-click system tray icon → Quit).
- Tiết kiệm RAM khi không dùng. Lần sau mở lại → `docker-compose up -d` để restart stack.

---

## 🔧 CÁC LỆNH HỮU ÍCH

```bash
# Status containers
docker ps                        # đang chạy
docker ps -a                     # tất cả (kể cả đã stop)

# Xem log
docker logs -f --tail 50 news-es
docker logs -f --tail 50 news-ch
docker logs -f --tail 50 news-kibana

# Vào shell trong container
docker exec -it news-es bash
docker exec -it news-ch bash

# Check RAM/CPU usage
docker stats

# Restart 1 container
docker restart news-es
```

---

## ⚠️ TROUBLESHOOTING NHANH

| Vấn đề | Giải pháp |
|---|---|
| `curl: (7) Failed to connect to localhost:9200` | ES chưa up xong — đợi 30s, check `docker logs news-es` |
| `Cannot connect to the Docker daemon` | Docker Desktop chưa chạy — mở lên |
| `BadRequestError: ... routing doesn't support values of type: VALUE_NULL` | ES 8.x issue — đã fix ở [02_index_to_es.py](main_process/02_index_to_es.py) |
| `no input files matched` | Chưa có NER output — chạy `python pre_dataset/02_ner.py` trước |
| ES `OOM` hoặc chậm | Giảm heap trong [docker-compose.yml](main_process/docker-compose.yml): `ES_JAVA_OPTS=-Xms512m -Xmx512m` |
| Port 9200/5601/9000 đã bị dùng | `docker-compose down` stack cũ, hoặc đổi port trong docker-compose.yml |

---

## 📌 TL;DR — chạy hằng ngày

```bash
# 1. Crawl + NER (stage 1)
python run_all.py

# 2. Index + analytics (stage 2)
cd main_process
docker-compose up -d                 # nếu Docker chưa up
python 01_clean_entities.py
python 02_index_to_es.py --reset
python 03_load_to_ch.py --reset
python 04_verify.py

# 3. Xong — tắt stack
docker-compose down
```
