-- Curator schema — migration 0040 (job_runs structured error code)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds job_runs.error_code so a consumer can branch on WHY a run failed without pattern-matching the
-- prose in job_runs.error. The scheduled-refresh worker pauses a user's schedule immediately when their
-- PSN link is dead, and until now it decided that by searching job_runs.error for the literal fragment
-- 'PlayStation Network link'. That coupled a control-flow decision to user-facing copy in a different
-- runtime: rewording the message degrades the immediate pause to the slower consecutive-failure path
-- silently, with nothing failing loudly.
--
-- Nullable with no default and no backfill: rows written before this migration legitimately have no
-- code, and a consumer must treat NULL as 'unclassified' rather than assume a category. The CHECK keeps
-- the vocabulary closed so a typo in a writer is rejected at the database rather than silently creating
-- a category nothing matches -- the same drop/add-constraint discipline 0008 used for status.

ALTER TABLE job_runs
    ADD COLUMN error_code TEXT;

ALTER TABLE job_runs
    ADD CONSTRAINT job_runs_error_code_check CHECK (error_code IS NULL OR error_code IN (
        'psn_link_expired', 'provider_key_rejected', 'provider_rate_limited', 'malformed_payload', 'unexpected'
        ));
