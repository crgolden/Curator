-- Curator schema — migration 0035 (index entitlement_snapshots.title_id)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Every cover-art lookup resolves through entitlement_snapshots.title_id (see
-- curator.catalog.cover_art.SQUARE_COVER_ART_SQL), joined from library_entries.title_id, which
-- migration 0023 added. That column has never been indexed: entitlement_snapshots carries indexes on
-- concept_id and product_id only, both predating the identifier fix 0023 made.
--
-- Without it the planner seq-scans the whole table once per row on the page. Paging bounds how many
-- scans run, not what each costs, so the total scales with page size times table size. Measured against
-- a real library, cost added by the lookup:
--
--     page 50 (the default):   +44 ms without the index, +0.6 ms with it
--     page 200 (the maximum): +382 ms without the index, +5.6 ms with it
--
-- Absolute figures move between runs under concurrent load; the ratio does not. The join key, not the
-- row count, is the problem -- the same lookup through concept_id was fast only because
-- idx_entitlement_snapshots_concept_id exists. entitlement_snapshots grows with every user's library,
-- so the unindexed form degrades over time even at a fixed page size.
--
-- Partial on the artwork predicate because every query using this index also requires artwork to be
-- present, and the rows with none are dead weight in it.

CREATE INDEX idx_entitlement_snapshots_title_id
    ON entitlement_snapshots (title_id)
    WHERE COALESCE(title_image_url, concept_icon_url, game_icon_url) IS NOT NULL;
