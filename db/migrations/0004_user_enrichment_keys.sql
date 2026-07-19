-- Curator schema — migration 0004 (bring-your-own-key RAWG/OpenCritic enrichment)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds per-user, optionally-provided RAWG/OpenCritic API keys (user_enrichment_keys) -- Curator never
-- provisions a shared key for either provider (it doesn't scale), so a user's own library enrichment only
-- happens if they've supplied their own key for that provider. Keys are encrypted with the same Fernet key
-- (CURATOR_TOKEN_KEY / curator.persistence.crypto.TokenCrypto) already used for PSN tokens at rest --
-- see curator.persistence.repository.Repository.upsert_link for the precedent.
--
-- Also adds:
--   - game_enrichment.rawg_enriched / opencritic_enriched: whether each provider found a genuine match for
--     a game, independent of whether that match carried a usable score (score_source alone would produce
--     false negatives -- a RAWG match with no Metacritic score is still a real RAWG match). Source of truth
--     for the per-title enrichment checkmarks in Librarian's /library page.
--   - job_runs.result_summary: a JSON payload of what a succeeded library-refresh run actually did (newly
--     enriched titles per provider, whether the OpenCritic top-up below stopped early) -- see
--     curator.jobs.repository.JobRunsRepository.mark_succeeded and GET /library/refresh/{run_id}.
--   - opencritic_pagination_cursor: OpenCritic's RapidAPI search endpoint is capped at 25 requests/day
--     (vs. 200/day for every other request type), so both the admin's catalog-wide re-scrape and a user's
--     own BYOK top-up deliberately never use it -- they page through the non-search catalog-listing
--     endpoint instead (curator.enrichment.opencritic_client.OpenCriticClient.fetch_platform_games, ported
--     from the zero-search-quota strategy in Tools/PlayStation/ps_opencritic.py). This table is the shared,
--     resumable cursor both callers advance cooperatively, so repeated runs make forward progress through
--     the catalog instead of only ever re-fetching the first alphabetical page.
--   - account_action_log gains two new action values for BYOK key add/remove auditing (detail = provider
--     name only, never the key value).

CREATE TABLE user_enrichment_keys
(
    identity_sub           UUID PRIMARY KEY REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    rawg_api_key_enc       BYTEA,
    opencritic_api_key_enc BYTEA,
    rawg_added_at          TIMESTAMPTZ,
    opencritic_added_at    TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE game_enrichment
    ADD COLUMN rawg_enriched       BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN opencritic_enriched BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE job_runs
    ADD COLUMN result_summary JSONB;

CREATE TABLE opencritic_pagination_cursor
(
    platform   TEXT PRIMARY KEY,
    next_skip  INT         NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE account_action_log
    DROP CONSTRAINT account_action_log_action_check;

ALTER TABLE account_action_log
    ADD CONSTRAINT account_action_log_action_check CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted', 'enrichment_key_added', 'enrichment_key_removed'
        ));
