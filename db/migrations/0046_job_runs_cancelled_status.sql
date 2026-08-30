-- Curator schema — migration 0046 (a terminal 'cancelled' status for job_runs)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- A run stuck in 'queued' — its queue message never delivered, or its worker down when it was published —
-- had no operator remedy at all: POST /enrichment/runs returns the stuck run's id instead of starting a
-- fresh one until 24h of staleness elapses, and no route removed it. The requested remedy was a DELETE.
-- It is a 'cancelled' status instead, because job_runs is the audit trail ExpiredLeaseReaper and every
-- operator query read: deleting the row destroys the only evidence the run ever existed, and 'failed'
-- would claim the job was attempted and broke, which is a different fact from an operator standing it down.
--
-- 'cancelled' is the THIRD terminal status, and every non-terminal predicate in both repos is spelled as
-- the complement of the terminal set (status NOT IN ('succeeded','failed')). Adding a value without
-- widening those leaves a cancelled run reading as active forever, which is worse than the stuck run this
-- migration exists to clear. Curator's find_active_run/find_active_global_run are widened in the same
-- change; Functions' JobRunsRepository.TryBeginDeliveryAsync is the other side and ships separately.
--
-- Deploy order, from the rule in AGENTS/Curator.md: this migration is applied to the target database
-- BEFORE any writer sends the new value. A Functions-first deploy is an outage of the writing job, not a
-- partial rollout — every write fails with 23514.
--
-- Drop-then-add rather than a second CHECK, following 0008/0040/0044. The DROP is deliberately not
-- IF EXISTS: a drifted constraint name must fail the migrate job loudly rather than silently leave the
-- five-value CHECK standing while a writer starts sending 'cancelled'.
--
-- No backfill. Nothing already in the table was cancelled, and reclassifying an old 'failed' row would be
-- inventing a fact about why it ended.

ALTER TABLE job_runs
    DROP CONSTRAINT job_runs_status_check;

ALTER TABLE job_runs
    ADD CONSTRAINT job_runs_status_check CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'rate_limited', 'cancelled'
        ));
