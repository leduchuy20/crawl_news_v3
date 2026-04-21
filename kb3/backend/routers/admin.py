"""
routers/admin.py — endpoint chỉ admin dùng được (Kịch bản 3).
"""
from __future__ import annotations

from typing import List, Optional

from clickhouse_driver import Client as CHClient
from elasticsearch import Elasticsearch
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.deps.auth import User, require_admin
from backend.deps.clients import get_ch, get_es
from backend.config import get_settings


router = APIRouter(prefix="/admin", tags=["admin"])


class StatsResponse(BaseModel):
    es_full_count: int
    es_public_count: int
    ch_articles: int
    ch_keyword_events: int
    ch_entity_events: int
    hidden_by_rbac: int


@router.get("/stats", response_model=StatsResponse)
def admin_stats(
    _: User = Depends(require_admin),
    es: Elasticsearch = Depends(get_es),
    ch: CHClient = Depends(get_ch),
):
    """Thống kê backend — chỉ admin thấy."""
    s = get_settings()
    full = es.count(index=s.ES_INDEX_FULL)["count"]
    pub = es.count(index=s.ES_INDEX_PUBLIC)["count"]
    arts = ch.execute("SELECT count() FROM news.articles")[0][0]
    kws = ch.execute("SELECT count() FROM news.keyword_events")[0][0]
    ents = ch.execute("SELECT count() FROM news.entity_events")[0][0]
    return StatsResponse(
        es_full_count=full,
        es_public_count=pub,
        ch_articles=arts,
        ch_keyword_events=kws,
        ch_entity_events=ents,
        hidden_by_rbac=full - pub,
    )


class SourceStat(BaseModel):
    source: str
    count: int


class SourcesResponse(BaseModel):
    results: List[SourceStat]


@router.get("/sources", response_model=SourcesResponse)
def list_sources(
    _: User = Depends(require_admin),
    ch: CHClient = Depends(get_ch),
):
    rows = ch.execute("""
        SELECT source, count() AS c
        FROM news.articles
        GROUP BY source
        ORDER BY c DESC
    """)
    return SourcesResponse(results=[SourceStat(source=s, count=c) for s, c in rows])


class CategoriesResponse(BaseModel):
    results: List[dict]


@router.get("/categories", response_model=CategoriesResponse)
def list_categories(ch: CHClient = Depends(get_ch)):
    """Public list (không cần admin) — frontend dùng để fill dropdown."""
    rows = ch.execute("""
        SELECT category_normalized, count() AS c
        FROM news.articles
        GROUP BY category_normalized
        ORDER BY c DESC
    """)
    return CategoriesResponse(results=[{"category": c, "count": n} for c, n in rows])
