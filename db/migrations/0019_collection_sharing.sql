-- Curator schema — migration 0019 (collection visibility, sharing, and follows)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- WP4: follow a specific collection (not just its owner), and share one via an unguessable link.
--
-- visibility replaces nothing -- the existing account-wide user_profiles.show_collections/is_public gate
-- (0005) still applies as an outer gate on GET /users/{sub}/collections; visibility is a *second*,
-- per-collection gate underneath it. 'private': never shareable, share_slug (if any) is inert.
-- 'unlisted': the share link works, but the collection is not returned by GET /users/{sub}/collections --
-- only reachable by whoever has the link. 'public': the share link works AND it's returned by that list
-- (subject to the account-wide gate still being open). Public discovery/search of collections by anyone
-- who doesn't have a link is explicitly out of scope -- see the workspace plan's "Out of scope" section.
--
-- share_slug is generated client-side (curator.collections.repository, via secrets.token_urlsafe) at
-- creation time for every collection regardless of visibility, matching how run_id/device_id are already
-- generated client-side elsewhere in this codebase -- not lazily on first "make shareable" edit, so
-- flipping visibility never needs special-case slug-generation logic. A private collection's slug is
-- simply inert: GET /public/collections/{share_slug} checks visibility != 'private' before returning
-- anything, so an unshared collection's slug being technically assigned leaks nothing.

ALTER TABLE collection_definitions
    ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'unlisted', 'public')),
    ADD COLUMN share_slug TEXT UNIQUE NULL;

CREATE INDEX idx_collection_definitions_share_slug ON collection_definitions (share_slug) WHERE share_slug IS NOT NULL;

CREATE TABLE collection_follows
(
    follower_sub  UUID NOT NULL REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    definition_id UUID NOT NULL REFERENCES collection_definitions (definition_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_sub, definition_id)
);

CREATE INDEX idx_collection_follows_definition_id ON collection_follows (definition_id);
