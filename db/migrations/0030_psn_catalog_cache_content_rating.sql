-- Caches PSN's own content rating alongside the other official-catalog fields.
--
-- PS Store is the source of truth for every field it publishes; RAWG and OpenCritic complement it and
-- never supersede it. Content rating was the one PSN-published enrichment signal with nowhere to land:
-- CatalogClient.title_concept() already returns content_rating/rating_authority on every lookup, but
-- psn_catalog_cache had no column for it, so it was discarded on the way in and game_enrichment.esrb
-- could only ever be filled from RAWG's esrb_rating.
--
-- Storing it here rather than reading it live on each enrichment run keeps the PSN-preferred value
-- available on a cache hit. Without that, precedence would silently depend on whether the row happened to
-- be cached -- PSN would win on a cold lookup and lose on a warm one, which is worse than either rule
-- applied consistently.
--
-- Both columns are nullable: rows cached before this migration have no rating recorded, and not every
-- title carries one (unrated, or a region whose authority PSN does not report).
ALTER TABLE psn_catalog_cache
    ADD COLUMN content_rating   TEXT,
    ADD COLUMN rating_authority TEXT;
