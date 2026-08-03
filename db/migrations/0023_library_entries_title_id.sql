-- Curator schema — migration 0023 (library_entries.title_id / psn_catalog_cache repurposed)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Fixes a product_id/title_id identifier mix-up in PSN official-catalog enrichment
-- (curator.enrichment.enrichment_service._resolve_psn_catalog). PSN's catalog "concepts" endpoint
-- (CatalogClient.title_concept) requires an npTitleId (e.g. "CUSA13505_00") per its own docstring, but it
-- was being called with the game's PSN product id instead (e.g.
-- "UP0700-CUSA13505_00-ELEVENELEVEN1111", the PS-Store-URL-style id) -- a different identifier PSN's own
-- data model treats as distinct (curator.psn.library_client's entitlement parsing captures
-- title_meta.get("titleId") and entry.get("productId") as separate fields). PSN's concepts endpoint
-- silently returns an empty/no-match result for the malformed id rather than erroring
-- (catalog_client.py's `concept = details[0] if details else {}` falls through to `{}`), so every field
-- _parse_title_concept extracts (star_rating, publisher, genres, ...) came back empty for every single
-- row -- confirmed directly: every existing psn_catalog_cache row has star_rating/publisher/genres all
-- null/empty. TRUNCATE runs first because none of that (wrong, empty) cached data is worth keeping --
-- the column it was keyed by is being repurposed to actually hold what its rows should have been keyed
-- by all along, so nothing survives the rename.
--
-- library_entries.title_id is new because the correct identifier
-- (curator.catalog.canonicalization_service.CanonicalGame.winning_title_id) was already resolved
-- correctly from PSN's entitlements response all along, but was deliberately transient -- its docstring
-- says so -- which was fine for the same-build-pass exact PS4 trophy-title lookup
-- (LibraryBuildOrchestrator.match_trophies) that already used it, but broke PSN catalog enrichment's
-- continuation-resume path (curator.app._library_refresh_continuation_handler), which re-reads a user's
-- games from library_entries after a rate limit without re-canonicalizing, and so had no way to recover
-- the identifier it needed. Persisting it here closes that gap.

TRUNCATE psn_catalog_cache;

ALTER TABLE psn_catalog_cache
    RENAME COLUMN product_id TO title_id;

ALTER TABLE library_entries
    ADD COLUMN title_id TEXT;
