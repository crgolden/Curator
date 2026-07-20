-- Curator schema — migration 0005 (user profiles)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds user_profiles -- the display half of the new social-profile feature (see follows in
-- 0006_follows.sql for the graph half). Each row is a set of opt-in visibility toggles a user controls
-- for their own public profile: whether the profile is public at all, and whether library/collections/
-- trophies/identity are individually shown on it. Default-private (is_public DEFAULT false), same
-- opt-in posture as psn_links.harvest_* added in 0002_psn_data_preferences.sql.
--
-- Every show_* toggle here is meaningless on its own -- trophies/identity additionally require the
-- matching psn_links.harvest_* flag to be enabled, and the route layer (curator.profile_routes) enforces
-- that AND at request time. This table only stores the display half of that decision; it never itself
-- grants Curator permission to harvest anything from PSN.
--
-- No row means "never visited profile settings" -- curator.persistence.profile_repository.ProfileRepository
-- .get_settings returns all-false defaults rather than 404ing, the same "always answerable" precedent
-- EnrichmentKeysRepository.get_status established in 0004_user_enrichment_keys.sql.

CREATE TABLE user_profiles
(
    identity_sub     UUID PRIMARY KEY REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    is_public        BOOLEAN     NOT NULL DEFAULT false,
    show_library     BOOLEAN     NOT NULL DEFAULT false,
    show_collections BOOLEAN     NOT NULL DEFAULT false,
    show_trophies    BOOLEAN     NOT NULL DEFAULT false,
    show_identity    BOOLEAN     NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
