-- Curator schema — migration 0067 (drop the two never-realized data-quality tables)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- data_quality_flags and data_quality_flag_games were declared in 0001 for a merge/dedup review queue that
-- never got a writer, a reader or a route in either runtime. The function they were meant to serve is
-- carried by canonicalization and merge in the ingestion runtime. Child table first, for the foreign key.

DROP TABLE data_quality_flag_games;
DROP TABLE data_quality_flags;
