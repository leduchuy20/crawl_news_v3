-- ==========================================================================
-- ClickHouse schema cho news analytics
--
-- Thiết kế:
-- 1. Bảng `articles`: metadata + content. Partition theo publish_date để query
--    time-based nhanh.
-- 2. Bảng `keyword_events`: long format (1 bài × N keywords → N rows) — đây
--    là bảng CHÍNH cho Kịch bản 2 Trending.
-- 3. Bảng `entity_events`: tương tự keyword_events nhưng cho NER entities.
-- 4. Materialized View `hourly_keyword_stats`: tự aggregate theo giờ, query
--    trending cực nhanh (không phải scan keyword_events).
-- ==========================================================================

CREATE DATABASE IF NOT EXISTS news;

-- --------------------------------------------------------------------------
-- articles: 1 row per article
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS news.articles;

CREATE TABLE news.articles
(
    id                  String,
    url                 String,
    title               String,
    content             String,
    published_at        DateTime64(0, 'UTC'),
    crawled_at          DateTime64(0, 'UTC'),
    publish_date        Date,
    publish_hour        UInt8,
    publish_dow         UInt8,

    source              LowCardinality(String),
    source_domain       LowCardinality(String),
    source_type         LowCardinality(String),
    category_raw        String,
    category_normalized LowCardinality(String),
    author              String,
    language            LowCardinality(String),

    content_length      UInt32,
    word_count          UInt32,
    has_full_content    UInt8,
    is_canonical        UInt8,
    dup_group_id        String,
    dup_count           UInt16,

    -- Sub-columns cho quick aggregate mà không cần join keyword_events
    keywords            Array(String),
    entity_texts        Array(String),
    entity_types        Array(LowCardinality(String))
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(publish_date)
ORDER BY (publish_date, source, category_normalized, id)
SETTINGS index_granularity = 8192;

-- --------------------------------------------------------------------------
-- keyword_events: long format — cho Kịch bản 2 Trending
-- 1 article × N keywords → N rows
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS news.keyword_events;

CREATE TABLE news.keyword_events
(
    article_id          String,
    keyword             String,
    event_time          DateTime64(0, 'UTC'),
    publish_date        Date,
    publish_hour        UInt8,
    source              LowCardinality(String),
    category_normalized LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(publish_date)
ORDER BY (publish_date, keyword, source)
SETTINGS index_granularity = 8192;

-- --------------------------------------------------------------------------
-- entity_events: long format — cho search entity-based + stats
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS news.entity_events;

CREATE TABLE news.entity_events
(
    article_id          String,
    entity_text         String,
    entity_type         LowCardinality(String),
    event_time          DateTime64(0, 'UTC'),
    publish_date        Date,
    source              LowCardinality(String),
    category_normalized LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(publish_date)
ORDER BY (publish_date, entity_type, entity_text)
SETTINGS index_granularity = 8192;

-- --------------------------------------------------------------------------
-- Materialized view: aggregate theo (hour, keyword, category)
-- Cho phép query trending cực nhanh với SummingMergeTree
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS news.hourly_keyword_stats;

CREATE TABLE news.hourly_keyword_stats
(
    publish_date        Date,
    publish_hour        UInt8,
    keyword             String,
    category_normalized LowCardinality(String),
    source              LowCardinality(String),
    mention_count       UInt32
)
ENGINE = SummingMergeTree(mention_count)
PARTITION BY toYYYYMM(publish_date)
ORDER BY (publish_date, publish_hour, keyword, category_normalized, source)
SETTINGS index_granularity = 8192;

DROP VIEW IF EXISTS news.hourly_keyword_stats_mv;

CREATE MATERIALIZED VIEW news.hourly_keyword_stats_mv TO news.hourly_keyword_stats AS
SELECT
    publish_date,
    publish_hour,
    keyword,
    category_normalized,
    source,
    count() AS mention_count
FROM news.keyword_events
GROUP BY publish_date, publish_hour, keyword, category_normalized, source;

-- --------------------------------------------------------------------------
-- Daily aggregate cho trending — coarser granularity, nhanh cho 7d/30d query
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS news.daily_keyword_stats;

CREATE TABLE news.daily_keyword_stats
(
    publish_date        Date,
    keyword             String,
    category_normalized LowCardinality(String),
    mention_count       UInt32
)
ENGINE = SummingMergeTree(mention_count)
PARTITION BY toYYYYMM(publish_date)
ORDER BY (publish_date, keyword, category_normalized)
SETTINGS index_granularity = 8192;

DROP VIEW IF EXISTS news.daily_keyword_stats_mv;

CREATE MATERIALIZED VIEW news.daily_keyword_stats_mv TO news.daily_keyword_stats AS
SELECT
    publish_date,
    keyword,
    category_normalized,
    count() AS mention_count
FROM news.keyword_events
GROUP BY publish_date, keyword, category_normalized;

-- --------------------------------------------------------------------------
-- Similar MV cho entity_events
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS news.daily_entity_stats;

CREATE TABLE news.daily_entity_stats
(
    publish_date        Date,
    entity_text         String,
    entity_type         LowCardinality(String),
    category_normalized LowCardinality(String),
    mention_count       UInt32
)
ENGINE = SummingMergeTree(mention_count)
PARTITION BY toYYYYMM(publish_date)
ORDER BY (publish_date, entity_type, entity_text)
SETTINGS index_granularity = 8192;

DROP VIEW IF EXISTS news.daily_entity_stats_mv;

CREATE MATERIALIZED VIEW news.daily_entity_stats_mv TO news.daily_entity_stats AS
SELECT
    publish_date,
    entity_text,
    entity_type,
    category_normalized,
    count() AS mention_count
FROM news.entity_events
GROUP BY publish_date, entity_text, entity_type, category_normalized;
