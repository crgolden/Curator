-- A game admitted from a PlayStation Store search had nowhere to keep the cover the search already
-- returned. Cover art resolves from entitlement_snapshots, which only a library refresh fills, and the
-- store walk's own cache (psn_catalog_cache) is keyed on an npTitleId that a store SEARCH never returns.
-- So a title added by hand rendered with no art at all, permanently: nothing that runs later can recover
-- an image that was discarded at admission, short of a second walk.
--
-- This column is the holder. It is on games rather than psn_catalog_cache because it is keyed by the only
-- identifier a search hit carries through admission -- the game itself -- and because the value is a
-- property of the catalogued game, not of a PSN title-id cache entry.
--
-- It is deliberately a FALLBACK, not a new source of truth: cover_art.SQUARE_COVER_ART_SQL prefers the
-- square entitlement artwork and only reaches here when PSN carries none, so a game the owner really has
-- an entitlement for keeps rendering exactly the art it rendered before this migration.

ALTER TABLE games
    ADD COLUMN IF NOT EXISTS store_cover_image_url TEXT;
