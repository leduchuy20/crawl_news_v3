"""
deps/clients.py — ES singleton + fresh ClickHouse client per request.

clickhouse-driver's Client keeps ONE TCP socket and is NOT thread-safe.
FastAPI runs sync endpoints in a threadpool, so a cached singleton bắn
PartiallyConsumedQueryError khi 2 request chạy song song (tab Trends bắn
7 request /trends/* parallel). Cách xử lý: yield 1 client mới mỗi request
rồi disconnect ở finally — overhead ~1ms trên localhost.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Generator

from elasticsearch import Elasticsearch
from clickhouse_driver import Client as CHClient

from backend.config import get_settings


@lru_cache
def get_es() -> Elasticsearch:
    s = get_settings()
    return Elasticsearch(s.ES_URL, request_timeout=30)


def new_ch_client() -> CHClient:
    """Create a brand-new ClickHouse client. Caller is responsible for disconnect()."""
    s = get_settings()
    return CHClient(
        host=s.CH_HOST,
        port=s.CH_PORT,
        user=s.CH_USER,
        password=s.CH_PASSWORD,
        database="default",
        settings={"use_numpy": False},
    )


def get_ch() -> Generator[CHClient, None, None]:
    """FastAPI dependency — fresh client per request, auto-disconnect."""
    client = new_ch_client()
    try:
        yield client
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
