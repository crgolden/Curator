-- Curator schema — migration 0010 (entitlement artwork + ingestion fidelity)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- PSN's entitlement payload carries roughly three times what ingestion was extracting. Two problems were
-- fixed alongside this migration in curator.library.ingestion_service / curator.psn.library_client:
--
--   1. entitlement_snapshots.raw exists so that a bug in extraction can never permanently lose a field
--      PSN sent, and CatalogRepository.record_pull has always accepted a raw= argument to fill it -- but
--      IngestionService.ingest never passed one, so every row stored the literal '{}'. It now passes the
--      verbatim per-entitlement JSON through.
--   2. sku_id and active_date have existed as columns since 0001_initial.sql but were absent from the
--      INSERT's column list, so they were NULL on every row ever written. They are now populated.
--
-- The columns added here are the fields the product actually needs and could not previously reach.
-- PSN returns THREE distinct artwork URLs per entitlement and the old mapping collapsed them into one
-- (titleMeta.imageUrl preferred, gameMeta.iconUrl as fallback, conceptMeta.iconUrl never read at all) --
-- then dropped even that survivor before persistence, so no entitlement-sourced artwork reached the
-- database. A shareable collection page is mostly cover art, so all three are kept verbatim now.
--
-- platform_ids comes from entitlementAttributes[].platformId, the only per-platform signal in the
-- payload. is_game distinguishes a game from an add-on/DLC entitlement.
--
-- NOTE: this cannot be backfilled. The data was never stored, so existing rows stay NULL until the user
-- runs a fresh pull.

ALTER TABLE entitlement_snapshots
    ADD COLUMN title_image_url  TEXT,
    ADD COLUMN game_icon_url    TEXT,
    ADD COLUMN concept_icon_url TEXT,
    ADD COLUMN is_game          BOOLEAN,
    ADD COLUMN platform_ids     TEXT[] NOT NULL DEFAULT '{}';

-- opencritic_cache.raw has the same defect entitlement_snapshots.raw had: the column was declared in
-- 0001_initial.sql but omitted from the INSERT's column list in curator.enrichment.repository, so it was
-- NULL for every row. That INSERT now writes it, matching the correct pattern rawg_cache already used.
-- No schema change is needed for that -- this comment records it so the next reader isn't misled.

-- Two further tables are worth flagging while in this area: psn_game_search_cache and
-- psn_player_search_cache have no INSERT anywhere in curator/src. Their only would-be writers
-- (CatalogClient.search_games / SocialClient.search_players) have no route and no internal caller, so
-- both tables are currently unpopulated by design-drift rather than intent. Left in place deliberately;
-- do not assume they contain data.
