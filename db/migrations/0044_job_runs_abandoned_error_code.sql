-- Curator schema — migration 0044 (job_runs error code for a reaped run)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- 0040 closed the error_code vocabulary at five values, none of which describes the one failure the
-- database itself never sees a writer for: a run whose lease expired and was reclaimed. ExpiredLeaseReaper
-- marks such a run failed with a prose error and leaves error_code NULL, and 0040's own header defines NULL
-- as 'unclassified'. So a reaped run and a genuinely unclassified failure -- an old row written before
-- 0040, or a writer that simply did not set a code -- are indistinguishable in job_runs today. Operators
-- currently tell them apart by matching the reaper's prose string, which is exactly the coupling of
-- control flow to user-facing copy that 0040 exists to remove.
--
-- 'abandoned' names that outcome. No backfill: rows already reaped keep error_code NULL and stay
-- unclassified, which is honest -- nothing in the row proves it was a reap rather than any other
-- uncoded failure, and inferring one from the prose here would re-enshrine the string match this is
-- replacing.
--
-- Drop-then-add rather than a second CHECK, the same discipline 0040 took from 0008: one constraint per
-- column stays readable in \d and cannot end up half-updated. The DROP is deliberately not IF EXISTS --
-- if the live constraint name has drifted, the migrate job must fail loudly rather than silently leave the
-- five-value CHECK standing while a writer starts sending 'abandoned'.

ALTER TABLE job_runs
    DROP CONSTRAINT job_runs_error_code_check;

ALTER TABLE job_runs
    ADD CONSTRAINT job_runs_error_code_check CHECK (error_code IS NULL OR error_code IN (
        'psn_link_expired', 'provider_key_rejected', 'provider_rate_limited', 'malformed_payload',
        'unexpected', 'abandoned'
        ));
