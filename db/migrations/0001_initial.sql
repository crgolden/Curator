-- Curator schema — migration 0001 (initial)
-- Target: PostgreSQL 17. Applied manually via psql (see TESTING.md) — there is no migration runner.
--
-- Design
-- ------
-- Curator is multi-user: many people authenticate through Duende IdentityServer (OIDC) and each links
-- their own PSN account. The schema splits along that line:
--
--   * Account layer (app_users, psn_links) — one row per authenticated user, keyed by Identity's
--     immutable `sub` claim (identity_sub). No email column anywhere in this schema — Curator never
--     learns or stores a user's email address (hard privacy tenet; email lives in Identity, not here).
--
--   * Ingestion layer (entitlement_pulls, entitlement_snapshots) — per-user, append-only raw capture of
--     what psnpy's `entitlements` call returned, so a bad enrichment/curation run can always be replayed
--     from the original PSN response rather than re-fetched.
--
--   * Shared catalog layer (games, game_concepts, game_name_overrides, game_enrichment, rawg_cache,
--     opencritic_cache, psn_store_cache, data_quality_flags, data_quality_flag_games) — deliberately
--     GLOBAL, with no identity_sub column. Two different users who both own Elden Ring should merge onto
--     the same `games` row and share one enrichment record — re-enriching per user would be wasteful and
--     would fragment curation-quality signals (data-quality flags, name overrides) that are properties of
--     the game, not of any one user's library.
--
--   * Curation-rule layer (exclusion_rules, franchise_rules, edition_ranks, genre_priority,
--     size_estimates) — global config-as-data driving the curation/rotation algorithm. Not user-specific
--     by design: the rules that decide "this is a media app, not a game" or "this pattern belongs to the
--     Final Fantasy franchise" apply the same way to every user's library.
--
--   * Per-user library layer (library_entries, library_exclusions, user_consoles, measured_sizes,
--     assignment_runs, game_assignments, console_installs) — back to identity_sub-keyed rows: each user's
--     own derived library (which shared `games` rows they own), their own consoles, and their own
--     rotation/assignment history.
--
-- Conventions
-- -----------
--   * pgcrypto's gen_random_uuid() backs every surrogate key.
--   * Every enum-like column is constrained inline with CHECK — no separate lookup tables for fixed
--     value sets.
--   * created_at / updated_at are TIMESTAMPTZ NOT NULL DEFAULT now() wherever the entity is mutable;
--     append-only tables get a single timestamp column instead (pulled_at, detected_at, fetched_at, ...).
--   * Indexes are added on every foreign key used for lookups: identity_sub on per-user tables, and the
--     natural child-table foreign keys (game_id, concept_id, pull_id, console_id, run_id, ...).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================================
-- Account layer
-- ============================================================================================

-- One row per authenticated Curator user. identity_sub is Identity's `sub` claim — a UUID minted by
-- IdentityServer, not generated here, so it is the primary key rather than a separate surrogate key.
CREATE TABLE app_users
(
    identity_sub  UUID PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ NULL
);

-- Each user's link to their PSN account. token_response_enc holds the psnpy token dict (access +
-- refresh tokens and their expiries), Fernet-encrypted before it ever reaches SQL — see
-- curator.persistence.crypto.TokenCrypto and curator.persistence.db_token_store.DbTokenStore.
-- No npsso column: the npsso cookie is a one-time bootstrap credential, never persisted. No email
-- column: same hard privacy tenet as app_users.
-- last_verified_at tracks when the identity_sub/PSN email match was last re-checked against a bearer
-- token (see curator.reverify.reverify_link) -- NULL until the first (re-)verification. A token issued
-- after this timestamp triggers a fresh PSN check; an older/same-vintage token does not.
CREATE TABLE psn_links
(
    identity_sub             UUID PRIMARY KEY REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    psn_account_id           TEXT,
    token_response_enc       BYTEA NOT NULL,
    access_token_expires_at  TIMESTAMPTZ,
    refresh_token_expires_at TIMESTAMPTZ,
    linked_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_verified_at         TIMESTAMPTZ
);

-- ============================================================================================
-- Ingestion layer (per-user, append-only)
-- ============================================================================================

-- One row per call to psnpy's entitlements endpoint (or a manual JSON import). Append-only: a pull is
-- never updated or deleted, only superseded by a later pull.
CREATE TABLE entitlement_pulls
(
    pull_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub UUID NOT NULL REFERENCES app_users (identity_sub),
    pulled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL CHECK (source IN ('curator-live', 'manual-json-import')),
    entry_count  INT NOT NULL
);

CREATE INDEX idx_entitlement_pulls_identity_sub ON entitlement_pulls (identity_sub);

-- One row per entitlement returned by a single pull — the raw, unprocessed shape of each entry, kept
-- alongside the full original JSON (raw) so a downstream bug in extraction never loses information.
CREATE TABLE entitlement_snapshots
(
    pull_id           UUID NOT NULL REFERENCES entitlement_pulls (pull_id),
    entitlement_id    TEXT NOT NULL,
    concept_id        TEXT,
    product_id        TEXT,
    sku_id            TEXT,
    title_id          TEXT,
    game_meta_name    TEXT,
    concept_meta_name TEXT,
    title_meta_name   TEXT,
    -- Populate from gameMeta.packageType (values 'PSGD' / 'PS4GD'). The old ingestion read the wrong
    -- key, gameMeta.type — keep reading packageType here.
    package_type      TEXT,
    active            BOOLEAN,
    active_date       TIMESTAMPTZ,
    raw               JSONB NOT NULL,
    PRIMARY KEY (pull_id, entitlement_id)
);

CREATE INDEX idx_entitlement_snapshots_concept_id ON entitlement_snapshots (concept_id);
CREATE INDEX idx_entitlement_snapshots_product_id ON entitlement_snapshots (product_id);

-- ============================================================================================
-- Shared catalog layer (global — deliberately NO identity_sub)
-- ============================================================================================

-- The canonical, de-duplicated game catalog shared across every user.
CREATE TABLE games
(
    game_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_title  TEXT NOT NULL,
    -- The matching key used when merging entitlements/concepts onto a game.
    normalized_title TEXT NOT NULL,
    franchise        TEXT,
    search_names     TEXT[] NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_games_normalized_title ON games (normalized_title);

-- Maps a PSN concept id (TEXT, as PSN issues it — not a UUID) to the game it belongs to. Merging two
-- concepts onto the same game by product_id alone is unsafe — Sony reuses product ids across genuinely
-- different games — so a product-id merge additionally requires an identical name (dual-signal merge;
-- enforced at the application layer, not by a constraint here).
CREATE TABLE game_concepts
(
    concept_id TEXT PRIMARY KEY,
    game_id    UUID NOT NULL REFERENCES games (game_id),
    product_id TEXT
);

CREATE INDEX idx_game_concepts_game_id ON game_concepts (game_id);

-- A manual correction of a concept's display name (e.g. PSN's own metadata is wrong or misleading).
CREATE TABLE game_name_overrides
(
    concept_id    TEXT PRIMARY KEY REFERENCES game_concepts (concept_id),
    override_name TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per game holding every enrichment signal used by curation/rotation scoring.
CREATE TABLE game_enrichment
(
    game_id                UUID PRIMARY KEY REFERENCES games (game_id),
    genre                  TEXT,
    subgenre               TEXT,
    release_year           INT,
    developer              TEXT,
    publisher              TEXT,
    esrb                   TEXT,
    multiplayer            BOOLEAN,
    -- RAWG's Metacritic-sourced score.
    critical_score         NUMERIC(5, 2),
    oc_score               NUMERIC(5, 2),
    oc_tier                TEXT,
    oc_percent_recommended NUMERIC(5, 2),
    psn_rating             NUMERIC(3, 2),
    psn_rating_count       INT,
    score_source           TEXT CHECK (score_source IN ('RAWG + OC', 'OC Only', 'RAWG Only', 'Manual')),
    manual_score_override  NUMERIC(5, 2),
    aaa_tier               TEXT CHECK (aaa_tier IN ('AAA', 'AA', 'Indie')),
    collection_tier        TEXT CHECK (collection_tier IN
                                        ('Essential', 'Strong Recommendation', 'Good', 'Niche', 'Archive')),
    enriched_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RAWG lookup cache, keyed by the same normalized_title used to match games. raw = NULL means a
-- confirmed no-match (distinct from "not yet looked up", which is simply an absent row) — so a
-- re-enrichment pass never re-queries RAWG for a title already known to have no RAWG entry.
CREATE TABLE rawg_cache
(
    normalized_title TEXT PRIMARY KEY,
    rawg_game_id     INT,
    raw              JSONB,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- OpenCritic lookup cache, keyed by OpenCritic's own game id.
CREATE TABLE opencritic_cache
(
    oc_game_id          INT PRIMARY KEY,
    name                TEXT NOT NULL,
    top_critic_score    NUMERIC(5, 2),
    tier                TEXT,
    percent_recommended NUMERIC(5, 2),
    raw                 JSONB,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PS Store product-page scrape cache, keyed by PSN product id.
-- Merge-not-clobber: a failed refetch must never null out prior good values — that rule is enforced at
-- the application layer (the write path merges new fields onto the existing row rather than replacing
-- it wholesale), not by anything in this schema.
CREATE TABLE psn_store_cache
(
    product_id   TEXT PRIMARY KEY,
    rating       NUMERIC(3, 2),
    rating_count INT,
    genres       TEXT[] NOT NULL DEFAULT '{}',
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A detected data-quality issue in the shared catalog (e.g. two concepts that look like the same game
-- under different product ids, or the reverse).
CREATE TABLE data_quality_flags
(
    flag_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flag_type   TEXT NOT NULL CHECK (flag_type IN
                                      ('same_title_different_product_id', 'same_product_id_different_title',
                                       'metadata_drift')),
    details     JSONB,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ NULL,
    reviewed_by TEXT,
    resolution  TEXT CHECK (resolution IN ('confirmed_distinct', 'manually_merged', 'ignored'))
);

-- The games implicated in a data-quality flag (usually two, for a suspected duplicate/split pair).
CREATE TABLE data_quality_flag_games
(
    flag_id UUID NOT NULL REFERENCES data_quality_flags (flag_id),
    game_id UUID NOT NULL REFERENCES games (game_id),
    PRIMARY KEY (flag_id, game_id)
);

CREATE INDEX idx_data_quality_flag_games_game_id ON data_quality_flag_games (game_id);

-- ============================================================================================
-- Curation-rule layer (global config-as-data)
-- ============================================================================================

-- Titles/patterns to drop entirely from curation (media apps mistakenly counted as games, F2P titles
-- not worth ranking, ad-hoc name patterns, or an explicit whitelist override keeping a title in).
CREATE TABLE exclusion_rules
(
    rule_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_type TEXT NOT NULL CHECK (rule_type IN ('media_app', 'f2p_title', 'name_pattern', 'whitelist')),
    pattern   TEXT NOT NULL,
    notes     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Regex patterns mapping a title to a franchise grouping, used for franchise-aware curation.
CREATE TABLE franchise_rules
(
    rule_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern   TEXT NOT NULL,
    franchise TEXT NOT NULL,
    priority  INT NOT NULL DEFAULT 0
);

-- Keyword -> rank used to pick the "best" owned edition of a game (e.g. "Game of the Year" outranks
-- "Standard").
CREATE TABLE edition_ranks
(
    keyword TEXT PRIMARY KEY,
    rank    INT NOT NULL
);

-- Genre -> priority weighting used by rotation/assignment scoring.
CREATE TABLE genre_priority
(
    genre    TEXT PRIMARY KEY,
    priority INT NOT NULL
);

-- Install-size estimates used when a game has no measured_sizes row yet. A per-title substring override
-- (title_pattern) wins over the generic aaa_tier/genre_class row when both could apply.
CREATE TABLE size_estimates
(
    estimate_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title_pattern TEXT,
    aaa_tier      TEXT CHECK (aaa_tier IN ('AAA', 'AA', 'Indie')),
    genre_class   TEXT,
    platform      TEXT NOT NULL CHECK (platform IN ('PS5', 'PS4')),
    size_gb       NUMERIC(7, 2) NOT NULL,
    CHECK (title_pattern IS NOT NULL OR aaa_tier IS NOT NULL)
);

-- ============================================================================================
-- Per-user library layer
-- ============================================================================================

-- A user's derived library: one row per game they own, with the winning entitlement/edition already
-- resolved.
CREATE TABLE library_entries
(
    identity_sub           UUID NOT NULL REFERENCES app_users (identity_sub),
    game_id                UUID NOT NULL REFERENCES games (game_id),
    native_ps5             BOOLEAN NOT NULL DEFAULT false,
    ps4_eligible           BOOLEAN NOT NULL DEFAULT false,
    owned_edition          TEXT,
    winning_entitlement_id TEXT,
    product_id             TEXT,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (identity_sub, game_id)
);

CREATE INDEX idx_library_entries_identity_sub ON library_entries (identity_sub);
CREATE INDEX idx_library_entries_game_id ON library_entries (game_id);

-- Games a user has explicitly chosen to exclude from curation (overrides the global exclusion_rules
-- for that one user).
CREATE TABLE library_exclusions
(
    identity_sub UUID NOT NULL REFERENCES app_users (identity_sub),
    game_id      UUID NOT NULL REFERENCES games (game_id),
    reason       TEXT NOT NULL,
    excluded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    excluded_by  TEXT,
    PRIMARY KEY (identity_sub, game_id)
);

CREATE INDEX idx_library_exclusions_identity_sub ON library_exclusions (identity_sub);
CREATE INDEX idx_library_exclusions_game_id ON library_exclusions (game_id);

-- A user's physical consoles, used as rotation/assignment targets.
CREATE TABLE user_consoles
(
    console_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub     UUID NOT NULL REFERENCES app_users (identity_sub),
    name             TEXT NOT NULL,
    platform         TEXT NOT NULL CHECK (platform IN ('PS5', 'PS4')),
    raw_capacity_gb  NUMERIC(8, 2) NOT NULL,
    update_buffer_gb NUMERIC(8, 2) NOT NULL DEFAULT 0,
    routing_genres   TEXT[] NOT NULL DEFAULT '{}',
    fill_order       INT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (identity_sub, name)
);

CREATE INDEX idx_user_consoles_identity_sub ON user_consoles (identity_sub);

-- History-retaining, per-platform install-size measurements. Per-platform (not just per-game) so a PS4
-- measurement never clobbers a PS5 one for the same game; history-retaining (measured_at is part of the
-- key) so a later re-measurement doesn't destroy the prior data point.
CREATE TABLE measured_sizes
(
    identity_sub UUID NOT NULL REFERENCES app_users (identity_sub),
    game_id      UUID NOT NULL REFERENCES games (game_id),
    platform     TEXT NOT NULL CHECK (platform IN ('PS5', 'PS4')),
    size_gb      NUMERIC(7, 2) NOT NULL,
    measured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (identity_sub, game_id, platform, measured_at)
);

CREATE INDEX idx_measured_sizes_identity_sub ON measured_sizes (identity_sub);
CREATE INDEX idx_measured_sizes_game_id ON measured_sizes (game_id);

-- One row per run of the assignment/rotation algorithm, capturing the config used so a run is always
-- explainable/reproducible after the fact.
CREATE TABLE assignment_runs
(
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub    UUID NOT NULL REFERENCES app_users (identity_sub),
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_snapshot JSONB NOT NULL
);

CREATE INDEX idx_assignment_runs_identity_sub ON assignment_runs (identity_sub);

-- Per-game outcome of one assignment run: which console it landed on (NULL = unassigned/rotation
-- bench), its collection/rotation status, and the scores that drove the decision.
CREATE TABLE game_assignments
(
    run_id           UUID NOT NULL REFERENCES assignment_runs (run_id),
    game_id          UUID NOT NULL REFERENCES games (game_id),
    console_id       UUID REFERENCES user_consoles (console_id),
    collection_status TEXT CHECK (collection_status IN ('Installed', 'Bench')),
    rotation_tier    TEXT CHECK (rotation_tier IN ('Tier 1', 'Tier 2', 'Tier 3', 'Tier 4')),
    composite_score  NUMERIC(5, 2),
    rank_score       INT,
    assigned_size_gb NUMERIC(7, 2),
    PRIMARY KEY (run_id, game_id)
);

CREATE INDEX idx_game_assignments_game_id ON game_assignments (game_id);
CREATE INDEX idx_game_assignments_console_id ON game_assignments (console_id);

-- Current install state of a game on a specific console (the live, mutable counterpart to the
-- historical game_assignments record).
CREATE TABLE console_installs
(
    console_id UUID NOT NULL REFERENCES user_consoles (console_id),
    game_id    UUID NOT NULL REFERENCES games (game_id),
    installed  BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (console_id, game_id)
);

CREATE INDEX idx_console_installs_game_id ON console_installs (game_id);
