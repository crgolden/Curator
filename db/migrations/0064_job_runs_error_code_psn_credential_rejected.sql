-- Curator schema — migration 0064 (job_runs error code for a rejected app credential)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- psn_link_expired means a user's own stored PSN token was rejected and the remedy is to re-link. The
-- admin enrichment run holds no user token: it authenticates with the app's own npsso cookies, and when
-- every one of them is rejected the remedy is rotating the secret, not re-linking anything. Reporting
-- that as psn_link_expired sent operators to the wrong fix. psn_credential_rejected names the app-side
-- failure.
--
-- Two-repo order: this migration applies in production before the writer that sends the value deploys;
-- the reverse fails every such write with 23514. Drop-then-add without IF EXISTS, following 0044.

ALTER TABLE job_runs
    DROP CONSTRAINT job_runs_error_code_check;

ALTER TABLE job_runs
    ADD CONSTRAINT job_runs_error_code_check CHECK (error_code IS NULL OR error_code IN (
        'psn_link_expired', 'provider_key_rejected', 'provider_rate_limited', 'malformed_payload',
        'unexpected', 'abandoned', 'psn_credential_rejected'
        ));
