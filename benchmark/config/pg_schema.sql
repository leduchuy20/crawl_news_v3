-- ==========================================================================
-- PostgreSQL schema cho benchmark — thiết kế FAIR để so sánh với ES/ClickHouse.
--
-- Postgres có 3 cơ chế cho full-text search:
--   1. tsvector + GIN index (tốt nhất cho FTS)
--   2. pg_trgm + GIN index (tìm substring, chậm hơn nhưng linh hoạt)
--   3. LIKE / ILIKE (baseline, chậm nhất)
-- Ta tạo đủ 3 để báo cáo có so sánh.
--
-- Cho trending/aggregation: Postgres dùng GROUP BY với B-tree index.
-- Cho entity search: dùng JSONB + GIN index.
-- ==========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() declared STABLE → không xài trực tiếp được trong GENERATED column.
-- Workaround chuẩn: wrap thành SQL function IMMUTABLE.
-- Ref: https://www.postgresql.org/docs/16/textsearch-features.html
CREATE OR REPLACE FUNCTION immutable_unaccent(text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
$$ SELECT public.unaccent('public.unaccent', $1) $$;

-- --------------------------------------------------------------------------
-- Main table: mirror của ES news_articles + ClickHouse news.articles
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS articles CASCADE;

CREATE TABLE articles (
    id                    TEXT PRIMARY KEY,
    url                   TEXT,
    title                 TEXT NOT NULL,
    content               TEXT NOT NULL,
    published_at          TIMESTAMPTZ NOT NULL,
    crawled_at            TIMESTAMPTZ,
    publish_date          DATE NOT NULL,
    publish_hour          SMALLINT,
    publish_dow           SMALLINT,

    source                TEXT NOT NULL,
    source_domain         TEXT,
    source_type           TEXT,
    category_raw          TEXT,
    category_normalized   TEXT NOT NULL,
    author                TEXT,
    language              TEXT DEFAULT 'vi',

    keywords              TEXT[],
    entities              JSONB,              -- [{text, type}, ...]

    content_length        INT,
    word_count            INT,
    has_full_content      BOOLEAN,
    dup_group_id          TEXT,
    is_canonical          BOOLEAN,
    dup_count             SMALLINT
);

-- --------------------------------------------------------------------------
-- Full-text search: tsvector column tự generate
-- Weight: title (A) > content (B) — giống ES "title^3"
-- --------------------------------------------------------------------------
ALTER TABLE articles ADD COLUMN fts tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(immutable_unaccent(title), '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(immutable_unaccent(content), '')), 'B')
    ) STORED;

-- --------------------------------------------------------------------------
-- Long-format table cho aggregation test (mirror ClickHouse keyword_events)
-- --------------------------------------------------------------------------
DROP TABLE IF EXISTS keyword_events CASCADE;

CREATE TABLE keyword_events (
    article_id          TEXT NOT NULL,
    keyword             TEXT NOT NULL,
    publish_date        DATE NOT NULL,
    publish_hour        SMALLINT,
    source              TEXT,
    category_normalized TEXT
);

DROP TABLE IF EXISTS entity_events CASCADE;

CREATE TABLE entity_events (
    article_id          TEXT NOT NULL,
    entity_text         TEXT NOT NULL,
    entity_type         TEXT NOT NULL,
    publish_date        DATE NOT NULL,
    source              TEXT,
    category_normalized TEXT
);

-- --------------------------------------------------------------------------
-- Indexes — tạo SAU khi INSERT data để insert nhanh, rồi VACUUM ANALYZE
-- (xem 02_load_pg.py — tạo index post-load)
-- --------------------------------------------------------------------------
-- Queries dưới chỉ là định nghĩa, sẽ chạy trong load script:
--
-- -- B-tree cho filter
-- CREATE INDEX idx_articles_publish_date  ON articles (publish_date);
-- CREATE INDEX idx_articles_source        ON articles (source);
-- CREATE INDEX idx_articles_category      ON articles (category_normalized);
-- CREATE INDEX idx_articles_is_canonical  ON articles (is_canonical);
--
-- -- GIN cho full-text search (tsvector)
-- CREATE INDEX idx_articles_fts           ON articles USING GIN (fts);
--
-- -- GIN cho trigram (LIKE '%x%' acceleration)
-- CREATE INDEX idx_articles_title_trgm    ON articles USING GIN (title gin_trgm_ops);
-- CREATE INDEX idx_articles_content_trgm  ON articles USING GIN (content gin_trgm_ops);
--
-- -- GIN cho entities JSONB containment
-- CREATE INDEX idx_articles_entities_gin  ON articles USING GIN (entities jsonb_path_ops);
--
-- -- GIN cho keywords array
-- CREATE INDEX idx_articles_keywords_gin  ON articles USING GIN (keywords);
--
-- -- Composite cho aggregation
-- CREATE INDEX idx_kwev_date_kw   ON keyword_events (publish_date, keyword);
-- CREATE INDEX idx_kwev_kw_cat    ON keyword_events (keyword, category_normalized);
-- CREATE INDEX idx_entev_date_type ON entity_events (publish_date, entity_type);
