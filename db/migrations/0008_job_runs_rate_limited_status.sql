-- Curator schema — migration 0008 (job_runs rate_limited status)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds 'rate_limited' to job_runs.status's CHECK constraint, for the new rate-limit-aware library-refresh
-- continuation workflow: a run that stops early on a RAWG/OpenCritic 429 is neither 'failed' (it isn't an
-- error -- it's an expected, self-resolving pause) nor 'succeeded' (the run isn't actually done yet). The
-- run is republished to curator-library-refresh-continuation (same run_id, via Service Bus's scheduled-
-- message feature) to resume once the limit lifts, repeating as many times as it takes -- see
-- curator.jobs.repository.JobRunsRepository.mark_rate_limited, and the republisher itself, which lives in
-- the Functions repo at Functions/Curator/Library/LibraryRefreshQueuePublisher.cs rather than in Curator.
--
-- Same drop-constraint/add-constraint pattern 0004_user_enrichment_keys.sql/0006_follows.sql already used
-- for account_action_log_action_check.

ALTER TABLE job_runs
    DROP CONSTRAINT job_runs_status_check;

ALTER TABLE job_runs
    ADD CONSTRAINT job_runs_status_check CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'rate_limited'
        ));
