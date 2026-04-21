"""
config.py — env-based configuration, loaded once at startup.
"""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    # Services
    ES_URL: str = os.getenv("ES_URL", "http://localhost:9200")
    CH_HOST: str = os.getenv("CH_HOST", "localhost")
    CH_PORT: int = int(os.getenv("CH_PORT", "9000"))
    CH_USER: str = os.getenv("CH_USER", "default")
    CH_PASSWORD: str = os.getenv("CH_PASSWORD", "")

    # Index / alias names
    ES_INDEX_FULL: str = os.getenv("ES_INDEX_FULL", "news_articles")     # admin sees everything
    ES_INDEX_PUBLIC: str = os.getenv("ES_INDEX_PUBLIC", "news_public")   # guest filtered alias

    # Auth
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-me")
    JWT_ALG: str = "HS256"
    JWT_EXPIRE_MIN: int = int(os.getenv("JWT_EXPIRE_MIN", "60"))

    # Demo users (thay bằng DB thật nếu production)
    # Password gốc dạng plain — sẽ được hash tự động khi backend khởi động
    # Override bằng env: ADMIN_PASSWORD, GUEST_PASSWORD
    DEMO_USERS_PLAIN: dict = {
        "admin": {
            "role": "admin",
            "password": os.getenv("ADMIN_PASSWORD", "admin123"),
        },
        "guest": {
            "role": "guest",
            "password": os.getenv("GUEST_PASSWORD", "guest123"),
        },
    }

    # Fields ẩn khỏi guest (Kịch bản 3)
    GUEST_EXCLUDE_FIELDS: list = ["author", "crawled_at", "url"]

    # CORS
    ALLOWED_ORIGINS: list = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        # Cho phép mọi origin khi dev — thay bằng domain cụ thể ở prod
        "*",
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
