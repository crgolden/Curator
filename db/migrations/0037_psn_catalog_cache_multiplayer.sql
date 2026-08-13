-- Caches PSN's own multiplayer signal alongside the other official-catalog fields.
--
-- game_enrichment.multiplayer has been derived by keyword-matching RAWG's community tags ("multiplayer",
-- "co-op", "online", "pvp", "cooperative") against the tag list. PSN states it outright: the concept
-- payload carries compatibilityNotices as typed pairs, including NO_OF_PLAYERS for local play and
-- NO_OF_NETWORK_PLAYERS / NO_OF_NETWORK_PLAYERS_PS_PLUS for online, so the answer is read rather than
-- inferred from whatever words a community tagger happened to use.
--
-- Nullable, and NULL means PSN published no player-count notice for the title -- deliberately distinct
-- from a stated single player. Only the former is a reason to consult another source; a title PSN says is
-- single-player must not be overridden by a RAWG tag that merely mentions "online".
--
-- Stored here rather than read live on each enrichment run for the same reason 0030 gave for
-- content_rating: without it, precedence would silently depend on whether the row happened to be cached,
-- so PSN would win on a cold lookup and lose on a warm one.

ALTER TABLE psn_catalog_cache
    ADD COLUMN multiplayer BOOLEAN;
