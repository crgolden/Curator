-- Curator schema — migration 0062 (promote the storefront price node to columns)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- 0047 kept the whole product node in raw so a later decision to use one of its fields would not cost a
-- second walk. price is that field: the catalog card, the detail page and a price sort all read it, and
-- reading JSON for a sort is what an index cannot serve. Parsed at walk time from basePrice and
-- discountedPrice ("$19.99"); "Free", "Included" and anything else unparseable stay NULL in the cents
-- columns while the flags keep what the gateway said. No locale column: the walk is en-US by
-- construction, and a column recording the client's own constant is debt on arrival.
--
-- price_fetched_at is set only when a walk carried a price node, so a row that predates this migration
-- reads as "no price known" rather than as free.

ALTER TABLE psn_catalog_cache
    ADD COLUMN price_is_free BOOLEAN,
    ADD COLUMN price_tied_to_subscription BOOLEAN,
    ADD COLUMN price_base_cents INTEGER,
    ADD COLUMN price_discounted_cents INTEGER,
    ADD COLUMN price_discount_text TEXT,
    ADD COLUMN price_fetched_at TIMESTAMPTZ;

CREATE INDEX idx_psn_catalog_cache_game_price
    ON psn_catalog_cache (game_id, price_fetched_at DESC)
    WHERE price_fetched_at IS NOT NULL;
