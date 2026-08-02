-- Curator schema — migration 0020 (rejected enrichment key tracking)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- WP6: a rejected RAWG/OpenCritic key no longer fails a user's library refresh --
-- curator.library.library_build_orchestrator.enrich_games degrades instead, disabling just that
-- provider for the rest of the run. This migration adds the columns that let the rejection outlive one
-- run: rawg_key_rejected_at / opencritic_key_rejected_at are set when a refresh reports that provider in
-- result_summary.rejected_providers, and cleared on the next successful PUT /me/enrichment-keys/{provider}
-- for that provider (which already validates the key live before persisting it -- a successful save is
-- proof the rejection no longer applies).
--
-- Also extends account_action_log's action CHECK constraint with 'enrichment_key_rejected' (same pattern
-- 0004_user_enrichment_keys.sql/0006_follows.sql used) -- see curator.audit.repository
-- .ACTION_ENRICHMENT_KEY_REJECTED. detail on these rows is the provider name only, matching
-- enrichment_key_added/enrichment_key_removed.

ALTER TABLE user_enrichment_keys
    ADD COLUMN rawg_key_rejected_at       TIMESTAMPTZ,
    ADD COLUMN opencritic_key_rejected_at TIMESTAMPTZ;

ALTER TABLE account_action_log
    DROP CONSTRAINT account_action_log_action_check;

ALTER TABLE account_action_log
    ADD CONSTRAINT account_action_log_action_check CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted', 'enrichment_key_added', 'enrichment_key_removed', 'followed', 'unfollowed',
        'enrichment_key_rejected'
        ));
