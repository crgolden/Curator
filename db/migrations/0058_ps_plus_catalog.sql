-- Curator schema — migration 0058 (PS Plus catalog membership, and the entitlement reward type as a column)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- A member's entitlements only ever see a title LEAVE the Game Catalog: a title rotating in does not enter
-- anyone's library until they claim it by hand, so "which titles do I still have to claim" needs the catalog
-- itself. These tables hold that catalog as Curator walks it: one row per category the operator chose, one
-- row per walk, and one row per (title, category) with the walk that first and last saw it.
--
-- The category table is data rather than a code allowlist because the ids come from the store site's own
-- URLs and the storefront publishes no enumeration; reporting_name_prefix is what lets a walk refuse a
-- category that has been repurposed under the same id instead of walking the wrong thing. There is no
-- enabled flag: nothing would write it, and a retired category is a migration deleting its row.
--
-- classification stays uncoerced TEXT because it is the vendor's own per-category vocabulary. raw keeps the
-- whole product node, on the same reasoning as 0047. Membership is not a column on psn_catalog_cache: it is
-- per category, it has a lifecycle (left_at), and that cache row is overwritten by every other walk.
--
-- left_at is set only by a walk that completed with no coverage shortfall; a partial walk never marks a
-- departure, and a title that reappears clears left_at while keeping first_seen_at.
--
-- reward_membership_type is a stored generated column over the entitlement payload the ingestion already
-- keeps in raw (rewardMeta.rewardMembershipType = 'PS_PLUS' marks an entitlement obtained through PS Plus),
-- so the report's SQL reads a column rather than JSON and the ingestion runtime needs no change: both
-- writers insert with an explicit column list and a generated column cannot be named in one.

CREATE TABLE ps_plus_catalog_categories (
    category_id           UUID PRIMARY KEY,
    tier                  TEXT NOT NULL CHECK (tier IN ('extra', 'premium')),
    locale                TEXT NOT NULL,
    reporting_name_prefix TEXT NOT NULL
);

INSERT INTO ps_plus_catalog_categories (category_id, tier, locale, reporting_name_prefix) VALUES
    ('3a7006fe-e26f-49fe-87e5-4473d7ed0fb2', 'extra',   'en-US', 'SPAR_GMA_PSGC'),
    ('8056ad23-7f30-485c-a628-b99f9d5aec5d', 'premium', 'en-US', 'SPAR_GMA_PSPLUS_CC');

CREATE TABLE ps_plus_catalog_walks (
    walk_id           UUID PRIMARY KEY,
    category_id       UUID NOT NULL REFERENCES ps_plus_catalog_categories,
    started_at        TIMESTAMPTZ NOT NULL,
    completed_at      TIMESTAMPTZ,
    reported_total    INTEGER,
    distinct_products INTEGER NOT NULL DEFAULT 0,
    stopped_reason    TEXT CHECK (stopped_reason IN ('query_rotated', 'filter_not_applied', 'no_products',
                                                    'page_budget_exhausted', 'category_renamed'))
);

CREATE INDEX idx_ps_plus_catalog_walks_category_completed
    ON ps_plus_catalog_walks (category_id, completed_at DESC);

CREATE TABLE ps_plus_catalog_memberships (
    title_id           TEXT NOT NULL,
    category_id        UUID NOT NULL REFERENCES ps_plus_catalog_categories,
    store_product_id   TEXT NOT NULL,
    classification     TEXT,
    first_seen_walk_id UUID NOT NULL REFERENCES ps_plus_catalog_walks,
    last_seen_walk_id  UUID NOT NULL REFERENCES ps_plus_catalog_walks,
    first_seen_at      TIMESTAMPTZ NOT NULL,
    last_seen_at       TIMESTAMPTZ NOT NULL,
    left_at            TIMESTAMPTZ,
    raw                JSONB NOT NULL,
    PRIMARY KEY (title_id, category_id)
);

CREATE INDEX idx_ps_plus_catalog_memberships_category_left
    ON ps_plus_catalog_memberships (category_id, left_at);

ALTER TABLE entitlement_snapshots
    ADD COLUMN reward_membership_type TEXT
        GENERATED ALWAYS AS (raw -> 'rewardMeta' ->> 'rewardMembershipType') STORED;

CREATE INDEX idx_entitlement_snapshots_ps_plus_reward
    ON entitlement_snapshots (identity_sub, title_id)
    WHERE reward_membership_type = 'PS_PLUS';
