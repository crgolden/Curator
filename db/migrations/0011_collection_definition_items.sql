-- Curator schema — migration 0011 (collections as stored membership lists)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Until now a "collection" was a stored predicate: collection_definitions held genre_filter/min_score/
-- aaa_tier_filter and nothing else, so the only way to learn which games were in a collection was to
-- re-run the algorithm against the owner's current library and hope it produced the same answer. That
-- makes a collection unshareable (a second viewer would re-evaluate against their own library, or not at
-- all) and unstable (the same collection silently changes membership as the owner's library or the
-- enrichment data moves underneath it).
--
-- collection_definition_items makes membership an explicit, materialized, ordered list -- the canonical
-- answer to "what is in this collection". The filter columns on collection_definitions stay, demoted to
-- provenance: they record how a collection was originally assembled and let the owner ask for a fresh
-- proposal (POST /collections/{id}/runs), but they are never re-evaluated to decide membership.
--
-- collection_runs/collection_items are deliberately left alone. They are run history -- "what the
-- algorithm proposed on this date, including what it rejected and why" -- and overloading them as
-- membership would conflate a suggestion with a decision.
--
-- rank comes from the caller's array position, so the owner's chosen ordering is the stored ordering.

ALTER TABLE collection_definitions
    ADD COLUMN description TEXT;

CREATE TABLE collection_definition_items
(
    definition_id UUID        NOT NULL REFERENCES collection_definitions (definition_id) ON DELETE CASCADE,
    game_id       UUID        NOT NULL REFERENCES games (game_id),
    rank          INTEGER     NOT NULL,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (definition_id, game_id)
);

-- The definition_id FK cascades so this table joins the chain 0009_fix_delete_cascades.sql established:
-- deleting an app_users row cascades to collection_definitions, which now cascades to here. A new child
-- table without that cascade would silently reintroduce the DELETE /me foreign-key violation 0009 fixed.
--
-- game_id deliberately does NOT cascade. games is the shared, cross-user catalog; nothing deletes from it
-- today, and if something ever does, a collection quietly losing members is the wrong outcome.
CREATE INDEX idx_collection_definition_items_game_id ON collection_definition_items (game_id);
