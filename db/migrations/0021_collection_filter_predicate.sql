-- Curator schema — migration 0021 (OR-capable collection filter predicate)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- WP8: filter_list_strategy.apply_filter_list's genre_filter/min_score/aaa_tier_filter are pure AND --
-- structurally unable to express the legacy "Criterion" classifier's rule, genre in SET or (genre ==
-- action and tier == indie). curator.collections.filter_predicate adds a small OR-capable predicate tree
-- (GenreIn/TierIn/ScoreAtLeast/And/Or) that a collection_definitions row can carry instead of the flat
-- fields, evaluated in place of them (not combined) by apply_filter_list when present.
--
-- Stored as JSONB, not normalized predicate tables: the existing genre_filter/min_score/aaa_tier_filter
-- columns are provenance only (WP2) and are never re-evaluated to decide a saved collection's membership
-- -- see this table's original header comment and curator.collections.repository.CollectionDefinition's
-- docstring -- so the tree does not need to be queryable, only round-trippable through
-- curator.collections.filter_predicate.parse_predicate/predicate_to_dict.

ALTER TABLE collection_definitions
    ADD COLUMN filter_predicate JSONB;
