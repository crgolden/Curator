-- A user's opt-in to Curator using their stored PSN token on a schedule they did not directly trigger.
-- Every other PSN read is either user-initiated or gated by a psn_links harvest_* preference; this is the
-- first thing Curator does with someone's credentials while they are not looking, so it gets its own row
-- rather than riding on an existing flag.
--
-- ps_plus_watch is a column here rather than a second table: watching PS Plus rotation is a narrowing of
-- the same scheduled pull, not an independent job.
--
-- ON DELETE CASCADE plus an explicit delete in DELETE /psn/link: a user who unlinks has withdrawn exactly
-- the consent this row records, and a schedule pointed at a dead token is the failure mode worth designing
-- out rather than discovering.
CREATE TABLE user_refresh_schedules
(
    identity_sub         UUID PRIMARY KEY REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    cadence              TEXT        NOT NULL CHECK (cadence IN ('weekly', 'monthly')),
    ps_plus_watch        BOOLEAN     NOT NULL DEFAULT false,
    next_run_at          TIMESTAMPTZ NOT NULL,
    last_run_at          TIMESTAMPTZ,
    consecutive_failures INTEGER     NOT NULL DEFAULT 0,
    paused_reason        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The worker claims due schedules by time, not by user.
CREATE INDEX idx_user_refresh_schedules_next_run_at ON user_refresh_schedules (next_run_at);
