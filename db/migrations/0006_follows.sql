-- Curator schema — migration 0006 (follow graph)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds follows -- a simple directed follow graph, first-party Curator data (not PSN-derived), which is
-- why follower/following counts and lists are always visible on a profile regardless of that profile's
-- user_profiles.is_public flag (see 0005_user_profiles.sql): the follow graph is Curator's own, PSN
-- consent has nothing to do with it.
--
-- The composite primary key (follower_sub, followed_sub) makes a follow idempotent at the database level
-- -- curator.persistence.follow_repository.FollowRepository.follow uses ON CONFLICT DO NOTHING rather than
-- checking existence first. follows_no_self_follow rejects a user following themselves; the route layer
-- (curator.profile_routes) additionally pre-checks this for a clean 400 rather than letting the
-- constraint violation surface as a raw database error.
--
-- Also extends account_action_log's action CHECK constraint with 'followed'/'unfollowed' (same pattern
-- 0004_user_enrichment_keys.sql used to add 'enrichment_key_added'/'enrichment_key_removed') -- see
-- curator.audit.repository's ACTION_FOLLOWED/ACTION_UNFOLLOWED. detail on these log rows is the other
-- user's identity_sub only, never PSN data.

CREATE TABLE follows
(
    follower_sub UUID        NOT NULL REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    followed_sub UUID        NOT NULL REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_sub, followed_sub),
    CONSTRAINT follows_no_self_follow CHECK (follower_sub <> followed_sub)
);

CREATE INDEX idx_follows_followed_sub ON follows (followed_sub);
CREATE INDEX idx_follows_follower_sub ON follows (follower_sub);

ALTER TABLE account_action_log
    DROP CONSTRAINT account_action_log_action_check;

ALTER TABLE account_action_log
    ADD CONSTRAINT account_action_log_action_check CHECK (action IN (
        'link_succeeded', 'link_failed', 'unlinked', 'library_refresh_requested', 'trophy_fetch',
        'account_deleted', 'enrichment_key_added', 'enrichment_key_removed', 'followed', 'unfollowed'
        ));
