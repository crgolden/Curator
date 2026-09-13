-- Curator schema — migration 0001 (initial)
-- Target: PostgreSQL 17. Applied by db/run_migrations.py in filename order.
--
-- Design
-- ------
-- Curator is multi-user: many people authenticate through Duende IdentityServer (OIDC) and each links
-- their own PSN account. The schema splits along that line:
--
--   * Account layer (app_users, psn_links, psn_test_accounts) — one row per authenticated user, keyed by
--     Identity's immutable `sub` claim (identity_sub). No email column anywhere in this schema — Curator
--     never learns or stores a user's email address (hard privacy tenet; email lives in Identity, not
--     here). psn_test_accounts is the DB-backed replacement for the folded-in psnpy's file-based
--     TestAccountStore — the mutation-safety wall's pinned test account, one per user, needs to be
--     visible across every Curator App Service instance, not just the one that pinned it.
--
--   * Ingestion layer (entitlement_pulls, entitlement_snapshots) — per-user, append-only raw capture of
--     what the folded-in psnpy entitlements client returns, so a bad enrichment/curation run can always
--     be replayed from the original PSN response rather than re-fetched. entitlement_id (the raw JSON
--     "id") is the only field proven unique across every raw entitlement — concept_id/product_id/sku_id
--     are grouping keys, not row identity, and have each been proven unreliable even for that (Sony
--     reuses a product_id across genuinely different games; splits one real product across two
--     concept_ids). games.game_id is a surrogate key for exactly that reason.
--
--   * Shared catalog layer (games, game_concepts, game_name_overrides, genres, game_enrichment,
--     rawg_cache, opencritic_cache, psn_catalog_cache, psn_game_search_cache, psn_player_search_cache,
--     data_quality_flags, data_quality_flag_games) — deliberately GLOBAL, with no identity_sub column.
--     Two different users who both own Elden Ring should merge onto the same `games` row and share one
--     enrichment record — re-enriching per user would be wasteful and would fragment curation-quality
--     signals (data-quality flags, name overrides) that are properties of the game, not of any one user's
--     library. Genre is a normalized reference (genres), not free text — a game's genre_id/subgenre_id
--     always resolves to a row in the one ranked genre list, closing the drift risk a free-text column
--     plus a separate unlinked priority table would allow.
--
--   * Curation-rule layer (exclusion_rules, franchise_rules, edition_ranks, publisher_tiers,
--     size_estimates, global_exclusions) — global config-as-data driving the curation/rotation algorithm.
--     Not user-specific by design: the rules that decide "this is a media app, not a game" or "this
--     pattern belongs to the Final Fantasy franchise" apply the same way to every user's library.
--     global_exclusions is the canonicalization-level permanent exclusion memory (distinct from
--     library_exclusions below, which is per-user) — once a concept is excluded here it never silently
--     regenerates on a later ingestion run, for any user.
--
--   * Per-user library layer (library_entries, library_exclusions, user_consoles, measured_sizes,
--     collection_definitions, collection_runs, collection_items, console_installs) — back to
--     identity_sub-keyed rows: each user's own derived library (which shared `games` rows they own),
--     their own consoles, and their own generated-collection history. collection_definitions/runs/items
--     generalize what a fixed "PS5 assignment" or "PS4 Criterion/Blockbuster assignment" used to be into
--     one reusable concept: a named or ad-hoc CollectionSpec (capacity-constrained bin-pack against a
--     specific console, or an unconstrained genre/score/tier filter list) that can be generated on demand
--     for any console or filter combination, not just two hardcoded drive names.
--
--   * No Postgres tables for trophy data, presence, the social graph, devices, or chat reads — trophy
--     summaries/titles are cached in Redis with a short TTL (time-decaying current-state data, not
--     something needing permanent history); presence/social/devices/chat stay live-proxy only, since PSN
--     is the source of truth and caching inherently-live data would just serve stale/wrong answers.
--
-- Conventions
-- -----------
--   * pgcrypto's gen_random_uuid() backs every surrogate key.
--   * Every enum-like column is constrained inline with CHECK — no separate lookup tables for fixed
--     value sets, except where the value set itself needs independent metadata (genres has a priority
--     and an active flag; publisher_tiers has a match_kind) rather than just being an enum.
--   * created_at / updated_at are TIMESTAMPTZ NOT NULL DEFAULT now() wherever the entity is mutable;
--     append-only tables get a single timestamp column instead (pulled_at, detected_at, fetched_at, ...).
--   * Indexes are added on every foreign key used for lookups: identity_sub on per-user tables, and the
--     natural child-table foreign keys (game_id, concept_id, pull_id, console_id, run_id, ...).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE app_users
(
    identity_sub  UUID PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ NULL
);

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

CREATE TABLE psn_test_accounts
(
    identity_sub   UUID PRIMARY KEY REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    psn_account_id TEXT NOT NULL,
    pinned_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE entitlement_pulls
(
    pull_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub UUID NOT NULL REFERENCES app_users (identity_sub),
    pulled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL CHECK (source IN ('curator-live', 'manual-json-import')),
    entry_count  INT NOT NULL
);

CREATE INDEX idx_entitlement_pulls_identity_sub ON entitlement_pulls (identity_sub);

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
    package_type      TEXT,
    active            BOOLEAN,
    active_date       TIMESTAMPTZ,
    raw               JSONB NOT NULL,
    PRIMARY KEY (pull_id, entitlement_id)
);

CREATE INDEX idx_entitlement_snapshots_concept_id ON entitlement_snapshots (concept_id);
CREATE INDEX idx_entitlement_snapshots_product_id ON entitlement_snapshots (product_id);

CREATE TABLE games
(
    game_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_title  TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    franchise        TEXT,
    search_names     TEXT[] NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_games_normalized_title ON games (normalized_title);

CREATE TABLE game_concepts
(
    concept_id TEXT PRIMARY KEY,
    game_id    UUID NOT NULL REFERENCES games (game_id),
    product_id TEXT
);

CREATE INDEX idx_game_concepts_game_id ON game_concepts (game_id);

CREATE TABLE game_name_overrides
(
    concept_id    TEXT PRIMARY KEY REFERENCES game_concepts (concept_id),
    override_name TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE genres
(
    genre_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name     TEXT NOT NULL UNIQUE,
    priority INT NOT NULL,
    active   BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE game_enrichment
(
    game_id                UUID PRIMARY KEY REFERENCES games (game_id),
    genre_id               UUID REFERENCES genres (genre_id),
    subgenre_id            UUID REFERENCES genres (genre_id),
    release_year           INT,
    developer              TEXT,
    publisher              TEXT,
    esrb                   TEXT,
    multiplayer            BOOLEAN,
    is_free_to_play        BOOLEAN,
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

CREATE INDEX idx_game_enrichment_genre_id ON game_enrichment (genre_id);
CREATE INDEX idx_game_enrichment_subgenre_id ON game_enrichment (subgenre_id);

CREATE TABLE rawg_cache
(
    normalized_title TEXT PRIMARY KEY,
    rawg_game_id     INT,
    raw              JSONB,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE psn_catalog_cache
(
    product_id      TEXT PRIMARY KEY,
    concept_id      TEXT,
    genres          TEXT[] NOT NULL DEFAULT '{}',
    star_rating     NUMERIC(3, 2),
    publisher       TEXT,
    release_date    DATE,
    cover_image_url TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE psn_game_search_cache
(
    normalized_query TEXT PRIMARY KEY,
    raw              JSONB,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE psn_player_search_cache
(
    normalized_query TEXT PRIMARY KEY,
    raw              JSONB,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE data_quality_flag_games
(
    flag_id UUID NOT NULL REFERENCES data_quality_flags (flag_id),
    game_id UUID NOT NULL REFERENCES games (game_id),
    PRIMARY KEY (flag_id, game_id)
);

CREATE INDEX idx_data_quality_flag_games_game_id ON data_quality_flag_games (game_id);

CREATE TABLE exclusion_rules
(
    rule_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_type  TEXT NOT NULL CHECK (rule_type IN ('media_app', 'f2p_title', 'name_pattern', 'whitelist')),
    pattern    TEXT NOT NULL,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE global_exclusions
(
    concept_id  TEXT PRIMARY KEY REFERENCES game_concepts (concept_id),
    reason      TEXT NOT NULL,
    excluded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE franchise_rules
(
    rule_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern   TEXT NOT NULL,
    franchise TEXT NOT NULL,
    priority  INT NOT NULL DEFAULT 0
);

CREATE TABLE edition_ranks
(
    keyword TEXT PRIMARY KEY,
    rank    INT NOT NULL
);

CREATE TABLE publisher_tiers
(
    tier_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern    TEXT NOT NULL,
    tier       TEXT NOT NULL CHECK (tier IN ('AAA', 'AA', 'Indie')),
    match_kind TEXT NOT NULL CHECK (match_kind IN ('exact', 'substring'))
);

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

CREATE TABLE collection_definitions
(
    definition_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub    UUID NOT NULL REFERENCES app_users (identity_sub),
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('capacity_fill', 'filter_list')),
    console_id      UUID REFERENCES user_consoles (console_id),
    genre_filter    TEXT[] NOT NULL DEFAULT '{}',
    min_score       NUMERIC(5, 2),
    aaa_tier_filter TEXT CHECK (aaa_tier_filter IN ('AAA', 'AA', 'Indie')),
    sort_order      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (identity_sub, name)
);

CREATE INDEX idx_collection_definitions_identity_sub ON collection_definitions (identity_sub);

CREATE TABLE collection_runs
(
    run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub  UUID NOT NULL REFERENCES app_users (identity_sub),
    definition_id UUID REFERENCES collection_definitions (definition_id),
    spec_snapshot JSONB NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_collection_runs_identity_sub ON collection_runs (identity_sub);

CREATE TABLE collection_items
(
    run_id             UUID    NOT NULL REFERENCES collection_runs (run_id),
    game_id            UUID    NOT NULL REFERENCES games (game_id),
    included           BOOLEAN NOT NULL,
    rank               INT,
    composite_score    NUMERIC(5, 2),
    rank_score         INT,
    size_gb            NUMERIC(7, 2),
    collection_status  TEXT CHECK (collection_status IN ('Installed', 'Bench')),
    rotation_tier      TEXT CHECK (rotation_tier IN ('Tier 1', 'Tier 2', 'Tier 3', 'Tier 4')),
    PRIMARY KEY (run_id, game_id)
);

CREATE INDEX idx_collection_items_game_id ON collection_items (game_id);

CREATE TABLE console_installs
(
    console_id UUID NOT NULL REFERENCES user_consoles (console_id),
    game_id    UUID NOT NULL REFERENCES games (game_id),
    installed  BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (console_id, game_id)
);

CREATE INDEX idx_console_installs_game_id ON console_installs (game_id);

CREATE TABLE job_runs
(
    run_id       UUID PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('library_refresh', 'enrichment')),
    identity_sub UUID REFERENCES app_users (identity_sub),
    status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_job_runs_identity_sub ON job_runs (identity_sub);
