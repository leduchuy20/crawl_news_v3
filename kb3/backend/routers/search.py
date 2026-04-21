"""
routers/search.py — Kịch bản 1: Search (Elasticsearch).

3 endpoint:
- GET /search              : full-text search title + content
- GET /search/by-entity    : tìm bài có nhắc entity cụ thể (PER/LOC/ORG)
- GET /articles/{id}       : xem chi tiết 1 bài
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from elasticsearch import Elasticsearch, NotFoundError
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.deps.clients import get_es
from backend.deps.auth import (
    User, get_optional_user, get_index_for_user, get_source_excludes_for_user,
)


router = APIRouter(tags=["search"])


# ====================================================================
# Response models
# ====================================================================
class Entity(BaseModel):
    text: str
    type: str


class ArticleSummary(BaseModel):
    id: str
    title: str
    source: str
    category_normalized: str
    publish_date: Optional[str] = None
    author: Optional[str] = None
    url: Optional[str] = None
    highlight: Optional[str] = None
    score: Optional[float] = None


class SearchResponse(BaseModel):
    query: str
    total: int
    took_ms: float
    index_used: str
    role: str
    results: List[ArticleSummary]


class ArticleDetail(BaseModel):
    id: str
    title: str
    content: str
    source: str
    source_domain: str
    category_raw: str
    category_normalized: str
    keywords: List[str]
    entities: List[Entity]
    published_at: Optional[str] = None
    publish_date: Optional[str] = None
    publish_hour: Optional[int] = None
    author: Optional[str] = None
    url: Optional[str] = None
    content_length: Optional[int] = None
    word_count: Optional[int] = None
    is_canonical: Optional[bool] = None
    dup_count: Optional[int] = None


# ====================================================================
# Helpers
# ====================================================================
def _hit_to_summary(hit: Dict[str, Any]) -> ArticleSummary:
    s = hit["_source"]
    # Prefer content highlight, fallback title
    snippet = None
    hl = hit.get("highlight", {})
    if hl.get("content"):
        snippet = hl["content"][0]
    elif hl.get("title"):
        snippet = hl["title"][0]

    return ArticleSummary(
        id=hit["_id"],
        title=s.get("title", ""),
        source=s.get("source", ""),
        category_normalized=s.get("category_normalized", ""),
        publish_date=s.get("publish_date"),
        author=s.get("author"),
        url=s.get("url"),
        highlight=snippet,
        score=hit.get("_score"),
    )


# ====================================================================
# Full-text search
# ====================================================================
@router.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1, description="Từ khoá tìm kiếm"),
    category: Optional[str] = Query(None, description="Filter category_normalized"),
    source: Optional[str] = Query(None, description="Filter source"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    size: int = Query(20, ge=1, le=100),
    es: Elasticsearch = Depends(get_es),
    user: User = Depends(get_optional_user),
):
    """Full-text search — kịch bản 1.a"""
    index = get_index_for_user(user)
    excludes = get_source_excludes_for_user(user)

    # Build query
    must: List[Dict[str, Any]] = [
        {
            "multi_match": {
                "query": q,
                "fields": ["title^3", "content"],
                "type": "best_fields",
            }
        }
    ]
    filters: List[Dict[str, Any]] = []
    if category:
        filters.append({"term": {"category_normalized": category}})
    if source:
        filters.append({"term": {"source": source}})
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["gte"] = date_from
        if date_to:
            rng["lte"] = date_to
        filters.append({"range": {"publish_date": rng}})

    body = {
        "query": {"bool": {"must": must, "filter": filters}},
        "highlight": {
            "fields": {
                "title": {"number_of_fragments": 0},
                "content": {"fragment_size": 150, "number_of_fragments": 1},
            },
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
        },
        "size": size,
    }

    t0 = time.time()
    res = es.search(index=index, body=body, source_excludes=excludes)
    took_ms = (time.time() - t0) * 1000

    hits = [_hit_to_summary(h) for h in res["hits"]["hits"]]
    return SearchResponse(
        query=q,
        total=res["hits"]["total"]["value"],
        took_ms=round(took_ms, 2),
        index_used=index,
        role=user.role,
        results=hits,
    )


# ====================================================================
# Entity-based search — KB1.b
# ====================================================================
@router.get("/search/by-entity", response_model=SearchResponse)
def search_by_entity(
    text: str = Query(..., min_length=1, description="Entity text (ví dụ: Iran, Trump)"),
    type: Optional[str] = Query(None, description="Entity type: PER | LOC | ORG | MISC"),
    category: Optional[str] = Query(None),
    size: int = Query(20, ge=1, le=100),
    es: Elasticsearch = Depends(get_es),
    user: User = Depends(get_optional_user),
):
    """Tìm bài có nhắc entity cụ thể — kịch bản 1.b (nested query)."""
    index = get_index_for_user(user)
    excludes = get_source_excludes_for_user(user)

    entity_must: List[Dict[str, Any]] = [{"term": {"entities.text": text}}]
    if type:
        entity_must.append({"term": {"entities.type": type}})

    query = {
        "bool": {
            "must": [
                {
                    "nested": {
                        "path": "entities",
                        "query": {"bool": {"must": entity_must}},
                    }
                }
            ],
            "filter": [],
        }
    }
    if category:
        query["bool"]["filter"].append({"term": {"category_normalized": category}})

    t0 = time.time()
    res = es.search(
        index=index,
        body={
            "query": query,
            "size": size,
            "sort": [{"publish_date": "desc"}],
        },
        source_excludes=excludes,
    )
    took_ms = (time.time() - t0) * 1000

    hits = [_hit_to_summary(h) for h in res["hits"]["hits"]]
    return SearchResponse(
        query=f"entity[{type or '*'}]={text}",
        total=res["hits"]["total"]["value"],
        took_ms=round(took_ms, 2),
        index_used=index,
        role=user.role,
        results=hits,
    )


# ====================================================================
# Article detail
# ====================================================================
@router.get("/articles/{article_id}", response_model=ArticleDetail)
def get_article(
    article_id: str,
    es: Elasticsearch = Depends(get_es),
    user: User = Depends(get_optional_user),
):
    """Chi tiết 1 bài. Guest sẽ bị ẩn field author/url."""
    index = get_index_for_user(user)
    excludes = get_source_excludes_for_user(user)
    try:
        res = es.get(index=index, id=article_id, source_excludes=excludes)
    except NotFoundError:
        raise HTTPException(404, f"Article {article_id} not found or not accessible")
    src = res["_source"]
    return ArticleDetail(
        id=res["_id"],
        title=src.get("title", ""),
        content=src.get("content", ""),
        source=src.get("source", ""),
        source_domain=src.get("source_domain", ""),
        category_raw=src.get("category_raw", ""),
        category_normalized=src.get("category_normalized", ""),
        keywords=src.get("keywords", []),
        entities=[Entity(**e) for e in src.get("entities", [])],
        published_at=src.get("published_at"),
        publish_date=src.get("publish_date"),
        publish_hour=src.get("publish_hour"),
        author=src.get("author"),
        url=src.get("url"),
        content_length=src.get("content_length"),
        word_count=src.get("word_count"),
        is_canonical=src.get("is_canonical"),
        dup_count=src.get("dup_count"),
    )
