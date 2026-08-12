-- Curator schema — migration 0034 (PSN social/chat write consent)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- AGENTS/DESIGNS.md §7 item 7. Two opt-in flags join the four harvest_* flags on psn_links, letting a user
-- allow Curator to make state-changing PSN calls on their behalf. Split in two rather than one combined
-- flag: a friend write affects one relationship PSN already models consent for, while a chat write is
-- publication in front of third parties who never opted into Curator.
--
-- Both default false. curator.psn.safety.MutationGuard enforces them live on every mutation, alongside a
-- re-check that the authenticated account is still the one linked to this identity_sub.
--
-- Also extends account_action_log's action CHECK with the five social/chat actions (same pattern
-- 0020_enrichment_key_rejection.sql used) -- see curator.audit.repository's ACTION_* constants. detail on
-- these rows is a target online id or a group id; chat_message_sent deliberately never carries the message
-- body, per 0003_account_action_log.sql's own prohibition on raw PSN data in detail.

ALTER TABLE psn_links
    ADD COLUMN allow_friend_writes BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN allow_chat_writes   BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE account_action_log
    DROP CONSTRAINT account_action_log_action_check;

ALTER TABLE account_action_log
    ADD CONSTRAINT account_action_log_action_check CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted', 'enrichment_key_added', 'enrichment_key_removed', 'followed', 'unfollowed',
        'enrichment_key_rejected',
        'friend_added', 'friend_removed', 'chat_group_created', 'chat_message_sent',
        'chat_membership_changed'
        ));
