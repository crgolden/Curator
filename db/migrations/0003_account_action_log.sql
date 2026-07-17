-- Curator schema — migration 0003 (account action log)
-- Target: PostgreSQL 17. Applied manually via psql (see TESTING.md) — there is no migration runner.
--
-- Adds account_action_log — a defensive audit trail of high-level actions Curator takes on a user's
-- behalf against their PSN link (link succeeded/failed, unlink, library refresh requested, trophy fetch,
-- account deletion, ...), not a transcript of every individual PSN API call. Purpose: if a user's account
-- is compromised by an unrelated third party and they dispute what Curator itself did with their
-- npsso/tokens, this table is the record that answers that question.
--
-- identity_sub deliberately carries NO foreign key to app_users and no ON DELETE CASCADE — DELETE /me
-- removes the account (migration 0001's cascade) but this table must survive that for the retention
-- window (GDPR Art. 17(3)(e): erasure does not override retention needed to establish/defend legal
-- claims). detail is a short human-readable summary (e.g. a failure reason) — never the npsso, never a
-- token, never raw PSN response data.
--
-- Retention: rows older than 1 year are purged by a scheduled job (see
-- curator.audit.repository.AccountActionLogRepository.purge_older_than); nothing in the request path
-- itself trims this table.

CREATE TABLE account_action_log
(
    log_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub UUID        NOT NULL,
    action       TEXT        NOT NULL CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted'
        )),
    detail       TEXT,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_account_action_log_identity_sub ON account_action_log (identity_sub);
CREATE INDEX idx_account_action_log_occurred_at ON account_action_log (occurred_at);
