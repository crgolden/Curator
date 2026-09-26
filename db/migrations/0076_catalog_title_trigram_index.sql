-- Curator schema — migration 0076 (trigram index behind the catalog title search)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- GET /catalog/games filters with g.canonical_title ILIKE '%term%', which no btree index can serve, so every
-- search was a sequential scan over games. A GIN trigram index serves exactly that predicate, and the store
-- crawl that admits every full game the PlayStation Store publishes multiplies the row count this scan pays
-- for. pg_trgm is a trusted extension, so the database owner creates it without superuser rights, the same
-- way 0001 creates pgcrypto.
--
-- The index needs no change to the query. Confirm it is used rather than assumed:
--   EXPLAIN SELECT game_id FROM games WHERE canonical_title ILIKE '%ghost%';
-- A term shorter than three characters yields no trigram and still scans; that is the extension's own bound,
-- not a defect in the predicate.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX idx_games_canonical_title_trgm ON games USING gin (canonical_title gin_trgm_ops);
