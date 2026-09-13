-- Curator schema — migration 0060 (retire exclusion_rules)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Created in 0001, populated by no migration, and read on every library build by the ingestion runtime
-- while nothing in this one ever queried it. A rule table that matches nothing goes stale unnoticed; the
-- structural non-game filters cover what a seed would have targeted, and games.content_kind (0061) takes
-- over the one rule type with a real purpose. global_exclusions and library_exclusions stay: both have
-- readers and a different purpose.
--
-- Deploy order: the ingestion runtime stops reading this table BEFORE this migration applies, because it
-- migrates ahead of its own deploy and neither pipeline sees the other. Dropping first would fail every
-- library build with 42P01 until the reader deployed.

DROP TABLE exclusion_rules;
