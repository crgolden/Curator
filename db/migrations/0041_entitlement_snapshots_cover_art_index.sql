-- Curator schema — migration 0041 (one cover-art index, ordered)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Migration 0039 added idx_entitlement_snapshots_title_id_with_art with a definition byte-identical to
-- 0035's idx_entitlement_snapshots_title_id: same column, same partial predicate. Two identical indexes
-- cost write amplification and planner time on every insert into a table that grows with every user's
-- library, and buy nothing.
--
-- Neither served the query they exist for. curator.catalog.cover_art.SQUARE_COVER_ART_SQL selects the
-- most recently seen artwork per title:
--
--     WHERE le_art.game_id = g.game_id
--       AND COALESCE(title_image_url, concept_icon_url, game_icon_url) IS NOT NULL
--     ORDER BY es.last_seen_at DESC
--     LIMIT 1
--
-- Adding last_seen_at DESC as a trailing key lets rows come back pre-ordered within each title_id, so
-- the subquery reads far fewer of them before the LIMIT is satisfied. It does not remove the sort
-- entirely: library_entries can match several title_ids for one game, so the planner still merges
-- across index ranges (verified with EXPLAIN — the Sort node remains above the nested loop). The win is
-- the index scan itself plus the partial predicate skipping artwork-less rows, not sort elimination.
--
-- Keeps the partial predicate 0035 established: every query using this index also requires artwork to be
-- present, so rows with none are dead weight in it.

DROP INDEX idx_entitlement_snapshots_title_id_with_art;

DROP INDEX idx_entitlement_snapshots_title_id;

CREATE INDEX idx_entitlement_snapshots_title_id_last_seen
    ON entitlement_snapshots (title_id, last_seen_at DESC)
    WHERE COALESCE(title_image_url, concept_icon_url, game_icon_url) IS NOT NULL;
