"""
main.py — FastAPI application entry point.

Run:
    cd app
    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings
from backend.routers import auth, search, trends, admin
from backend.deps.clients import get_es, get_ch


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ping services on startup để fail fast nếu config sai."""
    s = get_settings()
    try:
        es = get_es()
        if not es.ping():
            print(f"⚠ Elasticsearch không connect được: {s.ES_URL}")
        else:
            print(f"✓ Elasticsearch: {s.ES_URL}")
    except Exception as e:
        print(f"⚠ ES error: {e}")
    try:
        ch = get_ch()
        v = ch.execute("SELECT version()")[0][0]
        print(f"✓ ClickHouse {v}: {s.CH_HOST}:{s.CH_PORT}")
    except Exception as e:
        print(f"⚠ CH error: {e}")
    yield


app = FastAPI(
    title="News Analytics API",
    version="1.0.0",
    description=(
        "Backend cho đề tài 'Hệ thống phân tích xu hướng tin tức thời gian thực '. "
        "3 kịch bản: Search (ES), Trending (ClickHouse), RBAC (admin vs guest)."
    ),
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router)
app.include_router(search.router)
app.include_router(trends.router)
app.include_router(admin.router)


# ==========================================================================
# Root + health
# ==========================================================================
@app.get("/", tags=["meta"])
def root():
    return {
        "app": "News Analytics API",
        "docs": "/docs",
        "endpoints": {
            "auth": "/auth/login, /auth/me",
            "KB1_search": "/search, /search/by-entity, /articles/{id}",
            "KB2_trends": "/trends/top-keywords, /trends/hot, /trends/hourly, "
                          "/trends/top-entities, /trends/timeline, /trends/cross-source",
            "KB3_admin": "/admin/stats, /admin/sources, /admin/categories",
        },
        "demo_users": {"admin": "admin123", "guest": "guest123"},
    }


@app.get("/health", tags=["meta"])
def health():
    es = get_es()
    ch = get_ch()
    status = {"api": "ok"}
    try:
        status["es"] = "ok" if es.ping() else "down"
    except Exception as e:
        status["es"] = f"error: {e}"
    try:
        ch.execute("SELECT 1")
        status["ch"] = "ok"
    except Exception as e:
        status["ch"] = f"error: {e}"
    return status
