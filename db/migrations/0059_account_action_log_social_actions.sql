-- Curator schema — migration 0059 (two more social write actions in account_action_log)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- friend_request_sent: PUT /me/friends/{online_id} becomes accept-only and sending moves to its own route,
-- so the audit trail has to tell the two apart; PSN itself uses one call for both. chat_group_renamed:
-- renaming a group is neither a creation nor a membership change, and logging it under either would be a
-- lie. detail is the target online id or the group id, never a name or message body, per 0003.
--
-- Both actions are written only by this runtime, so no cross-repo deploy order applies. Drop-then-add
-- without IF EXISTS, following 0044 and 0056: a drifted constraint name must fail the migrate job loudly.

ALTER TABLE account_action_log
    DROP CONSTRAINT account_action_log_action_check;

ALTER TABLE account_action_log
    ADD CONSTRAINT account_action_log_action_check CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted', 'enrichment_key_added', 'enrichment_key_removed', 'followed', 'unfollowed',
        'enrichment_key_rejected',
        'friend_added', 'friend_removed', 'chat_group_created', 'chat_message_sent',
        'chat_membership_changed',
        'friend_request_sent', 'chat_group_renamed'
        ));
