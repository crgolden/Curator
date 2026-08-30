-- Curator schema — migration 0049 (per-provider provenance for the PS Store, alongside RAWG and OpenCritic)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- game_enrichment already records whether RAWG and OpenCritic contributed to a row (rawg_enriched,
-- opencritic_enriched, both added by 0004). The PS Store had no equivalent, so the enrichment run summary
-- could report rawg_enriched_count and opencritic_enriched_count but had nothing honest to report for PSN.
--
-- The tempting substitute is psn_rating IS NOT NULL, and it is wrong. psn_rating is a data field, not a
-- provenance flag: a concept fetch that returned genre, publisher and release date but no star rating
-- writes NULL there, and counting on that predicate reports the PS Store as having contributed nothing to
-- a row it in fact populated. Every field-derived predicate has the same defect, which is why this is a
-- column rather than a query.
--
-- Shape follows the two siblings exactly -- BOOLEAN NOT NULL DEFAULT false -- so the one Curator-side
-- writer that touches this table (catalog.repository.admit_store_game's bare
-- "INSERT INTO game_enrichment (game_id)") keeps working unchanged and lands the same "no provider has
-- contributed yet" value it already lands for RAWG and OpenCritic.
--
-- No backfill, deliberately. 0043 could backfill rawg_attempted_at because rawg_enriched = true was itself
-- proof RAWG had answered; nothing in this table proves the PS Store answered for a pre-existing row. The
-- only candidate predicate is psn_rating IS NOT NULL -- precisely the proxy this column exists to replace.
-- Every existing row therefore reads false, meaning "not recorded", and becomes true the next time the
-- enrichment pass resolves a concept for it.
--
-- No index. 0043 added one because rawg_attempted_at IS NULL became a worklist predicate; nothing selects
-- on psn_enriched. It is read as a per-row fact and counted in-process from the batch result.

ALTER TABLE game_enrichment
    ADD COLUMN psn_enriched BOOLEAN NOT NULL DEFAULT false;
