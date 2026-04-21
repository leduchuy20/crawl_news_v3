"""
deps/clients.py — singleton ES + ClickHouse clients.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from elasticsearch import Elasticsearch
from clickhouse_driver import Client as CHClient

from backend.config import get_settings


@lru_cache
def get_es() -> Elasticsearch:
    s = get_settings()
    return Elasticsearch(s.ES_URL, request_timeout=30)


@lru_cache
def get_ch() -> CHClient:
    s = get_settings()
    return CHClient(
        host=s.CH_HOST,
        port=s.CH_PORT,
        user=s.CH_USER,
        password=s.CH_PASSWORD,
        database="default",
        settings={"use_numpy": False},
    )
