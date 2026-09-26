-- Every use of a person's PSN token is recorded before it happens. A row is written 'started' before the
-- first PSN call and marked 'completed' or 'failed' afterwards, so a history write that cannot land stops
-- the action instead of letting it run unrecorded, and a row left 'started' is an attempt whose outcome
-- was never confirmed. Rows written before this migration recorded finished events, so they are
-- 'completed'.
--
-- The read actions name each live PSN read a route performs on the caller's token; trophy_fetch already
-- did. library_refresh_run is the Functions worker's own use of the token for a refresh run.
--
-- Deploy order: this migration before the Curator and Functions code that writes the new values or the
-- outcome column. Drop-then-add without IF EXISTS, following 0059.

ALTER TABLE account_action_log
    ADD COLUMN outcome TEXT NOT NULL DEFAULT 'completed'
        CONSTRAINT account_action_log_outcome_check CHECK (outcome IN ('started', 'completed', 'failed'));

ALTER TABLE account_action_log
    DROP CONSTRAINT account_action_log_action_check;

ALTER TABLE account_action_log
    ADD CONSTRAINT account_action_log_action_check CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted', 'enrichment_key_added', 'enrichment_key_removed', 'followed', 'unfollowed',
        'enrichment_key_rejected',
        'friend_added', 'friend_removed', 'chat_group_created', 'chat_message_sent',
        'chat_membership_changed',
        'friend_request_sent', 'chat_group_renamed',
        'link_requested', 'link_reverified', 'identity_fetch', 'presence_fetch', 'devices_fetch',
        'friend_requests_fetch', 'profile_lookup', 'store_search', 'device_link_check',
        'library_refresh_run'
        ));
