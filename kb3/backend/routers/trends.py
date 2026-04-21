"""
routers/trends.py — Kịch bản 2: Trending (ClickHouse).

Endpoints:
- GET /trends/top-keywords   : top keywords trong window N ngày
- GET /trends/hot            : spike detection so với period trước
- GET /trends/hourly         : phân bố bài theo giờ
- GET /trends/top-entities   : top PER/LOC/ORG entities
- GET /trends/timeline       : số bài theo ngày (chart)
- GET /trends/cross-source   : sự kiện được nhiều báo đưa
"""
from __future__ import annotations

import time
from typing import List, Optional

from clickhouse_driver import Client as CHClient
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.deps.clients import get_ch


router = APIRouter(prefix="/trends", tags=["trends"])


# ====================================================================
# Response models
# ====================================================================
class KeywordStat(BaseModel):
    keyword: str
    count: int


class HotKeyword(BaseModel):
    keyword: str
    count_now: int
    count_prev: int
    multiplier: float


class HourlyPoint(BaseModel):
    hour: int
    count: int


class EntityStat(BaseModel):
    text: str
    type: str
    count: int


class TimelinePoint(BaseModel):
    date: str
    count: int


class CrossSourceStat(BaseModel):
    keyword: str
    source_count: int
    total_mentions: int


class BaseTrendResponse(BaseModel):
    took_ms: float


class TopKeywordsResponse(BaseTrendResponse):
    window_days: int
    category: Optional[str]
    results: List[KeywordStat]


class HotKeywordsResponse(BaseTrendResponse):
    window_days: int
    results: List[HotKeyword]


class HourlyResponse(BaseTrendResponse):
    window_days: int
    results: List[HourlyPoint]


class TopEntitiesResponse(BaseTrendResponse):
    window_days: int
    entity_type: str
    results: List[EntityStat]


class TimelineResponse(BaseTrendResponse):
    category: Optional[str]
    results: List[TimelinePoint]


class CrossSourceResponse(BaseTrendResponse):
    window_days: int
    min_sources: int
    results: List[CrossSourceStat]


# ====================================================================
# Helpers
# ====================================================================
def _timed(ch_exec):
    def wrapper(sql, *a, **kw):
        t0 = time.time()
        rows = ch_exec(sql, *a, **kw)
        return rows, (time.time() - t0) * 1000
    return wrapper


# ====================================================================
# Endpoints
# ====================================================================
@router.get("/top-keywords", response_model=TopKeywordsResponse)
def top_keywords(
    days: int = Query(7, ge=1, le=365),
    category: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    ch: CHClient = Depends(get_ch),
):
    where = f"publish_date >= today() - {days}"
    params = {"limit": limit}
    if category:
        where += " AND category_normalized = %(cat)s"
        params["cat"] = category
    sql = f"""
        SELECT keyword, sum(mention_count) AS c
        FROM news.daily_keyword_stats
        WHERE {where}
        GROUP BY keyword
        ORDER BY c DESC
        LIMIT %(limit)s
    """
    t0 = time.time()
    rows = ch.execute(sql, params)
    took = (time.time() - t0) * 1000
    return TopKeywordsResponse(
        took_ms=round(took, 2),
        window_days=days,
        category=category,
        results=[KeywordStat(keyword=kw, count=c) for kw, c in rows],
    )


@router.get("/hot", response_model=HotKeywordsResponse)
def hot_keywords(
    window_days: int = Query(7, ge=1, le=60),
    min_now: int = Query(5, ge=1),
    limit: int = Query(20, ge=1, le=100),
    ch: CHClient = Depends(get_ch),
):
    """
    Keyword tăng đột biến so với kỳ trước (cùng window).
    Tính multiplier = count_now / count_prev. Filter count_now >= min_now để
    loại các từ khoá quá hiếm (không có ý nghĩa thống kê).
    """
    sql = """
        WITH
            cur AS (
                SELECT keyword, sum(mention_count) AS cnt_now
                FROM news.daily_keyword_stats
                WHERE publish_date >= today() - %(w)s
                GROUP BY keyword
            ),
            prev AS (
                SELECT keyword, sum(mention_count) AS cnt_prev
                FROM news.daily_keyword_stats
                WHERE publish_date >= today() - %(w2)s AND publish_date < today() - %(w)s
                GROUP BY keyword
            )
        SELECT c.keyword, c.cnt_now, coalesce(p.cnt_prev, 0),
               round(c.cnt_now / greatest(p.cnt_prev, 1), 2) AS mult
        FROM cur c LEFT JOIN prev p ON c.keyword = p.keyword
        WHERE c.cnt_now >= %(min_now)s
        ORDER BY mult DESC, c.cnt_now DESC
        LIMIT %(limit)s
    """
    t0 = time.time()
    rows = ch.execute(sql, {
        "w": window_days, "w2": window_days * 2,
        "min_now": min_now, "limit": limit,
    })
    took = (time.time() - t0) * 1000
    return HotKeywordsResponse(
        took_ms=round(took, 2),
        window_days=window_days,
        results=[
            HotKeyword(keyword=kw, count_now=cn, count_prev=cp, multiplier=m)
            for kw, cn, cp, m in rows
        ],
    )


@router.get("/hourly", response_model=HourlyResponse)
def hourly_distribution(
    days: int = Query(30, ge=1, le=365),
    ch: CHClient = Depends(get_ch),
):
    sql = """
        SELECT publish_hour, count() AS c
        FROM news.articles
        WHERE publish_date >= today() - %(d)s
        GROUP BY publish_hour
        ORDER BY publish_hour
    """
    t0 = time.time()
    rows = ch.execute(sql, {"d": days})
    took = (time.time() - t0) * 1000
    # Đảm bảo đủ 24 giờ (fill 0 cho giờ vắng data)
    by_hour = {h: c for h, c in rows}
    results = [HourlyPoint(hour=h, count=by_hour.get(h, 0)) for h in range(24)]
    return HourlyResponse(took_ms=round(took, 2), window_days=days, results=results)


@router.get("/top-entities", response_model=TopEntitiesResponse)
def top_entities(
    entity_type: str = Query("LOC", regex="^(PER|LOC|ORG|MISC)$"),
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    ch: CHClient = Depends(get_ch),
):
    sql = """
        SELECT entity_text, sum(mention_count) AS c
        FROM news.daily_entity_stats
        WHERE publish_date >= today() - %(d)s AND entity_type = %(t)s
        GROUP BY entity_text
        ORDER BY c DESC
        LIMIT %(limit)s
    """
    t0 = time.time()
    rows = ch.execute(sql, {"d": days, "t": entity_type, "limit": limit})
    took = (time.time() - t0) * 1000
    return TopEntitiesResponse(
        took_ms=round(took, 2),
        window_days=days,
        entity_type=entity_type,
        results=[EntityStat(text=t, type=entity_type, count=c) for t, c in rows],
    )


@router.get("/timeline", response_model=TimelineResponse)
def timeline(
    days: int = Query(30, ge=1, le=365),
    category: Optional[str] = None,
    ch: CHClient = Depends(get_ch),
):
    where = f"publish_date >= today() - {days}"
    params = {}
    if category:
        where += " AND category_normalized = %(cat)s"
        params["cat"] = category
    sql = f"""
        SELECT publish_date, count() AS c
        FROM news.articles
        WHERE {where}
        GROUP BY publish_date
        ORDER BY publish_date
    """
    t0 = time.time()
    rows = ch.execute(sql, params)
    took = (time.time() - t0) * 1000
    return TimelineResponse(
        took_ms=round(took, 2),
        category=category,
        results=[TimelinePoint(date=str(d), count=c) for d, c in rows],
    )


@router.get("/cross-source", response_model=CrossSourceResponse)
def cross_source(
    days: int = Query(7, ge=1, le=60),
    min_sources: int = Query(3, ge=2, le=20),
    limit: int = Query(20, ge=1, le=100),
    ch: CHClient = Depends(get_ch),
):
    sql = """
        SELECT keyword, uniqExact(source) AS sc, sum(mention_count) AS tm
        FROM news.daily_keyword_stats
        WHERE publish_date >= today() - %(d)s
        GROUP BY keyword
        HAVING sc >= %(ms)s
        ORDER BY sc DESC, tm DESC
        LIMIT %(limit)s
    """
    t0 = time.time()
    rows = ch.execute(sql, {"d": days, "ms": min_sources, "limit": limit})
    took = (time.time() - t0) * 1000
    return CrossSourceResponse(
        took_ms=round(took, 2),
        window_days=days,
        min_sources=min_sources,
        results=[CrossSourceStat(keyword=k, source_count=sc, total_mentions=tm)
                 for k, sc, tm in rows],
    )
