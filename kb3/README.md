# Bước 5 — Backend API + Frontend Demo

FastAPI backend + single-page HTML frontend minh họa 3 kịch bản của đề tài.

## 📂 Cấu trúc

```
kb3/
├── backend/
│   ├── main.py                # FastAPI entry point
│   ├── config.py              # Settings (env-based)
│   ├── deps/
│   │   ├── auth.py            # JWT + role-based access
│   │   └── clients.py         # ES + ClickHouse clients
│   ├── routers/
│   │   ├── auth.py            # /auth/login, /auth/me
│   │   ├── search.py          # KB1: /search, /search/by-entity, /articles/{id}
│   │   ├── trends.py          # KB2: /trends/* (6 endpoints)
│   │   └── admin.py           # KB3: /admin/stats, /admin/sources, /admin/categories
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── index.html             # Single-page HTML + Chart.js
├── docker-compose.yml         # Dùng chung network với main_process/
├── Makefile
└── README.md
```

## 🚀 Quick start

### Cách 1: Local dev (không docker)

```bash
# Terminal 1 — Backend
cd kb3
make install               # pip install
make dev                   # uvicorn --reload, port 8000

# Terminal 2 — Frontend
cd kb3
make frontend-serve        # python http.server, port 8080
```

Mở trình duyệt: **http://localhost:8080**

### Cách 2: Docker (production-like)

```bash
# Prerequisite: main_process stack đã chạy (xem ../main_process/README.md)
cd ../main_process && make up
cd ../kb3
make build
make up
```

Access:
- Frontend: **http://localhost:8080**
- Backend Swagger: **http://localhost:8000/docs**

## 🔐 Demo accounts

| Username | Password | Role |
|---|---|---|
| `admin` | `admin123` | Full access, thấy cả hidden fields |
| `guest` | `guest123` | Chỉ thấy canonical docs, ẩn author/url |

Có thể override bằng env: `ADMIN_PASSWORD`, `GUEST_PASSWORD`.

Hoặc không login → anonymous guest (cùng quyền như `guest`).

## 🎯 3 kịch bản được demo

### Kịch bản 1 — Search (Elasticsearch)
- **Full-text search** title + content với highlight
- **Entity search** (nested query): tìm bài có nhắc entity cụ thể
- Filter theo category
- Click vào bài → modal chi tiết với entities + keywords

### Kịch bản 2 — Trending (ClickHouse)
- Top keywords theo window (7d/14d/30d/90d), filter category
- **Hot spike**: keyword tăng đột biến so với period trước
- Timeline bài/ngày
- Phân bố bài theo giờ trong ngày
- Top LOC / PER entities
- **Cross-source coverage**: sự kiện được ≥3 báo đưa tin

### Kịch bản 3 — RBAC
- So sánh stats: index gốc vs alias `news_public`
- Compare cùng 1 article id giữa admin vs guest → thấy field nào bị ẩn
- `/admin/stats` chỉ admin truy cập được (guest bị 403)

## 📋 API endpoints (summary)

```
POST /auth/login              { username, password } → JWT
GET  /auth/me                 → current user

GET  /search?q=...&category=...&source=...&date_from=...&date_to=...
GET  /search/by-entity?text=...&type=PER|LOC|ORG
GET  /articles/{id}

GET  /trends/top-keywords?days=7&category=...
GET  /trends/hot?window_days=7
GET  /trends/hourly?days=30
GET  /trends/top-entities?entity_type=LOC&days=7
GET  /trends/timeline?days=30&category=...
GET  /trends/cross-source?days=7&min_sources=3

GET  /admin/stats             (admin only)
GET  /admin/sources           (admin only)
GET  /admin/categories        (public)
```

Full interactive docs: **http://localhost:8000/docs**

## ⚠️ Prerequisites

Backend cần:
- Elasticsearch có index `news_articles` và alias `news_public`
- ClickHouse có database `news` với 3 bảng + 3 MV

→ Chạy `cd ../main_process && make all` trước.

## 🔧 Environment variables

| Var | Default | Meaning |
|---|---|---|
| `ES_URL` | `http://localhost:9200` | ES endpoint |
| `CH_HOST` | `localhost` | ClickHouse host |
| `CH_PORT` | `9000` | ClickHouse port |
| `JWT_SECRET` | `dev-secret-change-me` | **Phải đổi khi production** |
| `JWT_EXPIRE_MIN` | `60` | Token lifetime |
| `ADMIN_PASSWORD` | `admin123` | Admin password |
| `GUEST_PASSWORD` | `guest123` | Guest password |
| `ES_INDEX_FULL` | `news_articles` | Admin index |
| `ES_INDEX_PUBLIC` | `news_public` | Guest alias |

## 🐛 Troubleshooting

**Frontend không gọi được API (CORS / network error):**
Check URL trong auth bar ở trên cùng. Mặc định `http://localhost:8000`. Nếu backend chạy port khác, sửa đây.

**Backend start không được:**
```bash
# Check ES + CH có chạy chưa
curl http://localhost:9200
curl http://localhost:8123/ping
```

**Index không có data:**
```bash
cd ../main_process && make all    # chạy full pipeline trước
```

**403 khi vào tab RBAC:**
Chưa login hoặc login với role `guest`. Login lại bằng `admin` / `admin123`.
