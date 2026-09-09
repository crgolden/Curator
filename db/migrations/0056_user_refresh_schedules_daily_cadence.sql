-- Curator schema — migration 0056 (a 'daily' cadence for user_refresh_schedules)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- The two cadences on offer were weekly and monthly. Daily is the shortest interval the enrichment budgets
-- support: OpenCritic's free RapidAPI plan is a per-DAY allowance (200 requests, 25 of them searches) that
-- refills at the day boundary, so anything shorter would spend a window that has not refilled. RAWG's free
-- allowance is monthly (20,000 requests), which a daily cadence draws down faster but never resets early.
-- Neither is enforced here; this migration only widens what the column will hold.
--
-- Deploy order, from the rule in AGENTS/REPOS/Curator.md: this migration is applied to the target database
-- BEFORE any writer sends the new value, which Curator's own pipeline guarantees by running Migrate ahead
-- of Deploy. The OTHER order is the one to watch, and it is not this file's to enforce: Functions'
-- ScheduledRefreshWorker computes every next_run_at after the first, so it must already know 'daily' before
-- this column will accept it. Functions-first is inert; Curator-first silently runs a daily schedule weekly.
--
-- Drop-then-add rather than a second CHECK, following 0008/0040/0044/0046. The DROP is deliberately not
-- IF EXISTS: a drifted constraint name must fail the migrate job loudly rather than silently leave the
-- two-value CHECK standing while a writer starts sending 'daily'. 0031 declared the CHECK inline, so
-- PostgreSQL named it user_refresh_schedules_cadence_check.
--
-- No backfill. Nothing already stored was daily, and rewriting an existing row's cadence would be
-- inventing a choice its owner never made.

ALTER TABLE user_refresh_schedules
    DROP CONSTRAINT user_refresh_schedules_cadence_check;

ALTER TABLE user_refresh_schedules
    ADD CONSTRAINT user_refresh_schedules_cadence_check CHECK (cadence IN (
        'daily', 'weekly', 'monthly'
        ));
