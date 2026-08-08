-- Links a cached PSN catalog row to the canonical game it describes, and records the storefront product
-- id so a catalog entry can deep-link to its official PS Store page.
--
-- Until now the only path from a game to its artwork ran through library_entries.title_id -- a per-user
-- table. That works for "this user's library" and "this user's collection", but a global catalog page has
-- no user to scope by, so it could never resolve a cover. The store backfill knows both halves (it resolves
-- the game while reading the product), so it writes the association directly.
--
-- game_id is nullable and not part of the key: rows cached by the older per-title enrichment path predate
-- the association, and a title whose game hasn't been canonicalized yet is still worth caching.
ALTER TABLE psn_catalog_cache
    ADD COLUMN game_id UUID REFERENCES games (game_id) ON DELETE CASCADE,
    ADD COLUMN store_product_id TEXT;

CREATE INDEX idx_psn_catalog_cache_game_id ON psn_catalog_cache (game_id);
