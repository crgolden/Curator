-- Curator schema — migration 0024 (collection_definitions exclude_installed_on)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds collection_definitions.exclude_installed_on: console ids whose currently-installed games
-- (console_installs.installed = true) are excluded from that definition's candidate pool entirely --
-- e.g. "what's left for my Vita that isn't already on my PS5". Closes WP8 reproduction-test gap #6 --
-- see AGENTS/PARKING_LOT.md and Tools/PlayStation/LIFECYCLE_AUDIT.md's "what it would take" list.
--
-- No FK on the array elements (Postgres has no native array-FK, same as genre_filter TEXT[] above having
-- no FK to genres); curator.collections.collection_orchestrator.CollectionOrchestrator validates every id
-- belongs to the caller's own consoles before it's ever used, and
-- CollectionsRepository.list_candidates' SQL join is scoped to the caller's own consoles regardless, so
-- an id that somehow got in without validation is still safely ignored rather than trusted.

ALTER TABLE collection_definitions
    ADD COLUMN exclude_installed_on UUID[] NOT NULL DEFAULT '{}';
