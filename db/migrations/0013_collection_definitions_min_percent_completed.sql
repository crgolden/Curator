-- Curator schema — migration 0013 (collection trophy-completion filter)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds the "% trophies completed" floor as a filter_list authoring option, stored alongside min_score/
-- aaa_tier_filter/genre_filter as provenance for POST /collections/{id}/runs to re-apply.
--
-- This column is the chosen threshold only. The per-game percentage it is compared against lives on
-- library_entries.trophy_percent_completed (0015), which is what lets this filter be expressed as a SQL
-- predicate in curator.collections.repository.CollectionsRepository.list_candidates rather than by
-- fetching every game's trophy data and filtering in Python afterwards. 0014 persists the
-- np_communication_id that links a library entry to its PSN trophy title in the first place, since no
-- exact identifier relates a catalog game_id to PSN's trophy-title id.
ALTER TABLE collection_definitions
    ADD COLUMN min_percent_completed SMALLINT CHECK (min_percent_completed BETWEEN 0 AND 100);
