-- Curator schema — migration 0025 (WP13: global measured-size cache)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Replaces measured_sizes (0001_initial.sql), a per-user, per-measurement history table that was never
-- written by any code path -- CollectionsRepository.list_candidates only ever SELECTed from it (see
-- collection_orchestrator.py's measured_size_gb short-circuit of the live install-size estimator), and
-- no route or job ever INSERTed a row. A real install size doesn't vary by which user is asking -- Elden
-- Ring's PS5 install size is the same physical fact for every owner -- so history-per-user was the wrong
-- shape from the start: it made the eventual "let a user report what they actually measured" feature
-- either duplicate the same number across every owner or, worse, only ever apply to the one user who
-- happened to record it, when every other owner's size_estimates fallback could have used it too.
--
-- game_measured_sizes is global and upserted, one row per (game_id, platform), mirroring game_enrichment's
-- own shape rather than a growing history: the previous measurement is genuinely superseded, not kept for
-- trend analysis, so there is nothing a second row for the same key would preserve that an UPDATE doesn't.
--
-- recorded_by is an accountability trail, not ownership -- "any authenticated user may write" (WP13's
-- settled design, see AGENTS/PARKING_LOT.md) means this is catalog-wide contributed data, the same trust
-- model as a RAWG/OpenCritic API key contribution. It is therefore ON DELETE SET NULL, not CASCADE: a
-- contributor deleting their account must not silently delete a measured size every other owner's
-- capacity_fill collections may already be sized against.
CREATE TABLE game_measured_sizes
(
    game_id     UUID NOT NULL REFERENCES games (game_id),
    platform    TEXT NOT NULL CHECK (platform IN ('PS5', 'PS4')),
    size_gb     NUMERIC(7, 2) NOT NULL,
    recorded_by UUID REFERENCES app_users (identity_sub) ON DELETE SET NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, platform)
);

CREATE INDEX idx_game_measured_sizes_recorded_by ON game_measured_sizes (recorded_by);

DROP TABLE measured_sizes;
