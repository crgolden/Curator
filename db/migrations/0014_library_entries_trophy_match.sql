-- Curator schema — migration 0014 (persisted trophy-title match per library entry)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- 0001_initial.sql's header explicitly says trophy data lives in Redis, not Postgres -- this migration is
-- a narrow, deliberate exception to that: np_communication_id is an IDENTITY fact (which PSN trophy title
-- this library entry corresponds to), not trophy DATA. Identity is stable and worth persisting once.
--
-- 0015 goes on to persist the completion percentage as well, and explains there why 0001's reasoning does
-- not survive contact with a filterable library. Earned-trophy lists and the trophy summary do stay
-- Redis-cached and are still never written to Postgres.
--
-- Why this exists: there is no exact identifier linking a catalog game to PSN's trophy-title id
-- (npCommunicationId) -- library_entries.winning_entitlement_id is PSN's entitlement_id, a different id
-- namespace. curator.psn.trophy_completion fuzzy-matches trophy title names against catalog titles to
-- work around that, but recomputing that match on every GET /library / collection request means an
-- O(games x titles) string-similarity pass on every read. Ingestion time (curator.library
-- .library_build_orchestrator's new match_trophies stage, run once per POST /library/refresh) is where
-- this belongs instead: match once, persist the result, and let reads become a cheap lookup by this
-- column plus an exact curator.psn.trophy_client.TrophyClient.trophy_titles_for_title() call.
--
-- trophy_match_attempted_at exists so "matched" / "tried, found nothing" / "never tried" stay distinguishable
-- -- without it, a title with genuinely no trophy set (an app, an F2P title) would be re-fuzzy-matched on
-- every single future refresh forever, since np_communication_id would stay NULL either way.
ALTER TABLE library_entries
    ADD COLUMN np_communication_id TEXT,
    ADD COLUMN trophy_match_method TEXT CHECK (trophy_match_method IN ('exact', 'fuzzy')),
    ADD COLUMN trophy_match_attempted_at TIMESTAMPTZ;
