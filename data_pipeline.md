# Pipeline Dữ Liệu — News Article Dataset

## Tổng quan Flow

```
[CRAWL]  rss_articles.jsonl  +  html_articles.jsonl
              ↓ build_dataset.py
[MERGE]      articles_final*.jsonl
              ↓ pre_dataset/01_cleanup.py
[CLEAN]      articles_cleaned*.jsonl
              ↓ pre_dataset/02_ner.py
[NER]        articles_ner*.jsonl
              ↓ main_process/01_clean_entities.py
[FINAL]      articles_ready*.jsonl  ← input cho ES + ClickHouse
```

Có **5 giai đoạn**, mỗi giai đoạn vừa biến đổi field, vừa lọc bớt record không hợp lệ.

---

## Giai đoạn 1 — Raw từ Crawler

**Source:** `rss_crawler.py` + `html_crawler.py`
**Output:** `data/rss_articles.jsonl` và `data/html_articles.jsonl`

Dataset gốc nhất, sinh ra bởi `crawler_core.Article` dataclass (`crawler_core.py:113-135`). Mỗi dòng JSON gồm **17 trường**:

| Trường | Kiểu | Mô tả |
|---|---|---|
| `id` | `str` | `md5(normalize_url(url))` — khoá dedup duy nhất xuyên suốt pipeline |
| `url` | `str` | URL sau `normalize_url`: bỏ fragment `#...` + bỏ trailing slash |
| `title` | `str` | Tiêu đề bài (đã `clean_content`: unescape HTML entities, strip boilerplate, gom whitespace) |
| `content` | `str` | Body bài sau khi extract (RSS = enrich full HTML hoặc fallback summary; HTML = trafilatura/JSON-LD/site extractor) |
| `published_at` | `str` | ISO-8601 UTC, parse từ `published_parsed`/`pubDate`/`updated` của RSS hoặc JSON-LD/meta của HTML |
| `crawled_at` | `str` | ISO-8601 UTC thời điểm crawl |
| `source` | `str` | Tên báo chuẩn ("vnexpress", "dantri", "tuoitre"…) — map qua `DOMAIN_TO_SOURCE` |
| `source_domain` | `str` | Domain rút từ URL ("vnexpress.net", đã lower + strip www.) |
| `source_type` | `str` | `"rss"` hoặc `"html"` — phân biệt 2 kênh thu thập |
| `category_raw` | `str` | Category gốc đúng cách báo đăng ("Thế giới", "the-the-thao", "thegioi-209"…) |
| `category_normalized` | `str` | Map về taxonomy chung qua `CATEGORY_MAP` 11 nhóm (world, sports, business…), không match → `"other"` |
| `author` | `str` | Tên tác giả nếu extractor lấy được, nhiều khi rỗng hoặc bị fallback bằng tên source (sẽ fix ở bước cleanup) |
| `language` | `str` | Cứng `"vi"` |
| `keywords` | `list[str]` | Union của RSS tag (`entry.tags[].term`) + keywords trong JSON-LD articleBody/meta keywords |
| `entities` | `list[dict]` | Luôn `[]` ở giai đoạn này — sẽ điền ở bước NER |
| `content_length` | `int` | `len(content)` |
| `has_full_content` | `bool` | `True` nếu `content_length >= 500` (là bài có body thật, không phải summary RSS) |

### Filter ngay tại Crawler — `is_article_valid` (`crawler_core.py:601-614`)

- Bỏ bài nếu **title rỗng**, hoặc **không có `published_at`**, hoặc **content ngắn hơn ngưỡng**:
  - `MIN_CONTENT_LENGTH_LOOSE = 100` cho RSS
  - `MIN_CONTENT_LENGTH_STRICT = 300` cho HTML
- `JsonlStore` dedup ngay theo `id`: bài nào trùng URL đã có trong bất cứ partition nào của file đó đều bị bỏ → resume sau crash không ghi trùng.

---

## Giai đoạn 2 — Merge 2 nguồn

**Source:** `build_dataset.py`
**Output:** `data/articles_final*.jsonl`

Vai trò: gộp HTML + RSS thành dataset thống nhất.

**Schema không thêm field mới**, vẫn 17 field y như `Article`. Việc xảy ra:

1. Đọc cả 2 file (kể cả tất cả partition đã rotate), aggregate theo `id`.
2. Nếu một bài có ở cả 2 nguồn, hàm `_merge` (`build_dataset.py:82-95`) **giữ bản có content dài hơn** (thường là HTML), đồng thời **union keywords** từ cả 2.
3. Sau đó re-chạy `clean_content` lần nữa trên `title` và `content` (xử lý double-encoded entity từ data crawl cũ), re-tính `content_length` và `has_full_content`.
4. Cuối cùng re-validate qua `is_article_valid` với threshold `LOOSE = 100` chars; bài nào ngắn hơn vẫn bị loại.
5. Chỉ append bài **MỚI** vào output (đã có trong `articles_final*.jsonl` thì giữ nguyên, không overwrite).

---

## Giai đoạn 3 — Cleanup metadata + Dedup cross-source

**Source:** `pre_dataset/01_cleanup.py`
**Output:** `data/articles_cleaned*.jsonl`
**Chế độ:** **FULL REBUILD** mỗi lần (xoá hết partition cũ → đọc lại toàn bộ → ghi lại)

Đây là bước **thêm nhiều field nhất** và **lọc mạnh nhất**.

### a. Fix Author

`fix_author()` (line 174-183): nếu `author` trùng `source` ("vnexpress" == "vnexpress") hoặc nằm trong `KNOWN_SOURCE_NAMES` → reset về `""`. Đây là fix cho bug fallback ở crawler cũ.

### b. Mở rộng `category_normalized`

Gọi `normalize_category_extended()` với `CATEGORY_MAP_EXTENDED` (~100 mapping, gấp đôi map cũ): nhặt được nhiều slug/tiếng Việt có dấu hơn nên tỉ lệ `"other"` giảm đáng kể.

### c. Dedup theo title-hash (gộp bài cùng sự kiện từ nhiều báo)

Thêm **3 field mới**:

| Trường | Kiểu | Mô tả |
|---|---|---|
| `dup_group_id` | `str` | md5 12 ký tự của normalized title (bỏ dấu, lower, bỏ punctuation). `""` nếu bài không trùng ai. |
| `is_canonical` | `bool` | Trong cùng group, bài có `has_full_content=True` + `content_length` lớn nhất được gắn `True`, còn lại `False`. |
| `dup_count` | `int` | Số bài trong group (≥1). |

### d. Thêm 4 derived field cho time-series (ClickHouse cần)

| Trường | Kiểu | Mô tả |
|---|---|---|
| `publish_date` | `str` | `YYYY-MM-DD`: ngày publish theo giờ VN (UTC+7) |
| `publish_hour` | `int` | 0–23: giờ publish VN |
| `publish_dow` | `int` | 0–6: thứ trong tuần, 0=Mon |
| `word_count` | `int` | `len(content.split())` |

### e. Filter cứng

- `content_length < 200` → bỏ (mặc định `--min-content-len 200`).
- Đây là tầng lọc "rác" cuối cùng trước khi đưa qua NER vì NER tốn ~500MB RAM/process.

Record được **sort theo `published_at`** trước khi ghi → output có thứ tự thời gian.

---

## Giai đoạn 4 — NER (Named Entity Recognition)

**Source:** `pre_dataset/02_ner.py`
**Output:** `data/articles_ner*.jsonl`
**Chế độ:** **INCREMENTAL** — scan ID đã có `entities` trong output rồi chỉ chạy cho phần còn lại (đỡ chạy lại model nặng)

Điểm khác duy nhất so với input: field `entities` từ `[]` được populate.

Chạy `underthesea.ner()` trên:
- Toàn bộ `title`
- 5000 ký tự đầu của `content` (giới hạn `MAX_CONTENT_CHARS_FOR_NER` để tránh chậm)

Output NER ở dạng BIO tag (`B-PER`, `I-LOC`…) được gom lại thành chuỗi entity hoàn chỉnh dạng `{"text": "...", "type": "PER|LOC|ORG|MISC"}`.

### Filter ở bước NER

- Length **2–60** ký tự (`MIN_ENTITY_LEN` / `MAX_ENTITY_LEN`)
- Dedup theo `(text.lower(), type)` trong cùng bài

Số trường không đổi — vẫn là **24 field** như sau cleanup (17 + 3 dedup + 4 time-series), chỉ là `entities` không còn rỗng.

---

## Giai đoạn 5 — Clean entities + keywords → articles_ready

**Source:** `main_process/01_clean_entities.py`
**Output:** `data/articles_ready*.jsonl`
**Chế độ:** **FULL REBUILD**

Đây là dataset **cuối cùng** nạp vào Elasticsearch + ClickHouse + PostgreSQL.

Bước này **không thêm field**, chỉ làm sạch nội dung 2 field nhiễu nhất sau NER.

### Clean entities — `clean_entity()` (line 186-241)

Mỗi entity đi qua **7 tầng lọc** theo thứ tự:

| # | Tầng | Mô tả |
|---|---|---|
| 1 | `normalize_text` | Strip punctuation đầu/cuối, gom whitespace |
| 2 | Length check | 2–50 ký tự (chặt hơn NER vì gì >50 ký tự thường là tokenizer gom nhầm 2 entity) |
| 3 | Reject pure-numeric | `"1234"`, `"12,3"` |
| 4 | Reject date-like | Regex `^(năm\|tháng\|ngày\|tuần\|giờ\|quý\|thế\s kỷ)\s*\d` (vd "năm 2026", "tháng 3") |
| 5 | Reject currency/percent | Chứa `%`/`‰` hoặc kết thúc bằng `USD`/`VND`/`đồng`/`tỷ`/`triệu`/`nghìn` |
| 6 | Reject common generic words | `COMMON_WORDS_REJECT`: bộ, điểm, người, ngày, ông, bà, anh, đây, đó, trên, dưới… |
| 7 | Re-classify + strip prefix | Strip prefix admin/title và force type |

#### Re-classify chi tiết

- Nếu text bắt đầu bằng `xã`, `huyện`, `tỉnh`, `tp.`, `phía bắc`… → strip prefix và **force type = LOC** (dedup "xã Sơn Cẩm" với "Sơn Cẩm")
- Nếu text bắt đầu bằng `ông`, `bà`, `tổng thống`, `hlv`, `giáo sư`… → strip và **force type = PER**
- Re-check length sau strip, re-check common word, dedup `(text.lower(), type)`

### Clean keywords — `clean_keyword()` (line 256-272)

Cùng các tầng 1–6 ở trên, **cộng thêm reject** `KEYWORD_CATEGORY_REJECT` (~100 từ):

| Nhóm | Ví dụ |
|---|---|
| Tên category tiếng Việt | "Thế giới", "Xã hội", "Kinh tế", "Thể thao"… |
| Bản tiếng Anh | "world", "sports"… |
| Slug | "the-gioi", "xa-hoi"… |
| Tag nav/UI generic | "tin tức", "video", "podcast", "góc nhìn", "xã luận"… |
| Regional tag | "ASEAN", "châu á", "chau-a-tbd"… |

Cuối cùng **dedup keyword theo lowercase**.

---

## Schema cuối cùng — `articles_ready`

Sau toàn bộ pipeline, mỗi record có đúng **24 trường**:

```json
{
  "id": "a941805bd131e05bae9a55c1a15d7f62",
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
  "author": "...",
  "language": "vi",
  "keywords": ["..."],
  "entities": [{"text": "...", "type": "PER|LOC|ORG"}],
  "content_length": 3218,
  "has_full_content": true,
  "dup_group_id": "...",
  "is_canonical": true,
  "dup_count": 2,
  "publish_date": "2026-02-03",
  "publish_hour": 20,
  "publish_dow": 1,
  "word_count": 542
}
```

### Phân bổ 24 trường

| Nhóm | Số lượng | Field |
|---|---|---|
| Gốc từ crawler | 17 | `id` → `has_full_content` |
| Dedup cross-source | 3 | `dup_group_id`, `is_canonical`, `dup_count` |
| Time-series VN | 3 | `publish_date`, `publish_hour`, `publish_dow` |
| Thống kê | 1 | `word_count` |
| **Tổng** | **24** | |

---

## Tóm tắt "Loại bỏ những gì" qua từng tầng

| Tầng | Loại bỏ |
|---|---|
| **Crawler** (`is_article_valid`) | Title rỗng, không có ngày publish, content < 100 (RSS) / 300 (HTML) ký tự |
| **Crawler** (`JsonlStore`) | Bài trùng URL (`id` đã có trong bất kỳ partition nào) |
| **`build_dataset.py`** | Bài trùng id cross-source (giữ bản content dài hơn), bài fail `is_article_valid` lần nữa |
| **`01_cleanup.py`** | Bài có `content_length < 200`; bài duplicate title-hash bị đánh dấu `is_canonical=false` (không xoá, dùng cho RBAC) |
| **`02_ner.py`** | Entity rỗng / dài >60 / ngắn <2 ký tự; entity trùng `(text, type)` |
| **`01_clean_entities.py`** | **Entity:** số thuần, ngày tháng, %, currency, common word, prefix admin/title bị strip<br>**Keyword:** tên category ("Thế giới"…), nav label ("tin tức"…), regional tag, common word |

### Lưu ý

- Bài "rác" thực ra **không bị xoá ở stage 5** — chỉ entity/keyword nhiễu trong bài bị xoá.
- Bài duplicate vẫn còn (qua `is_canonical=false`), để Stage 3 lọc bằng alias `news_public` cho user guest.
- Bản `is_canonical=false` còn dùng được khi muốn xem **coverage cross-source** (bao nhiêu báo đã đưa cùng sự kiện), nên project giữ lại thay vì xoá hẳn.
