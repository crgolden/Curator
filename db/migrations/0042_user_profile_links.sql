-- Curator schema — migration 0042 (user-declared PS profile links)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Lets a user surface their profile on other PlayStation sites from their Librarian profile.
--
-- The site is an allowlist, not a free-form URL, and that is the whole security design. A stored URL
-- rendered into an [href] for OTHER users to click is a persistent-XSS vector, and defending it means
-- getting scheme validation, javascript: rejection and rel= right forever. Storing a site key plus a
-- handle instead removes the class: the only user-supplied value is the handle, it is constrained to a
-- charset by CHECK, and profile_link_sites.url_template -- which no user can write -- builds the URL.
--
-- profile_link_sites is a seeded lookup table rather than a CHECK vocabulary because the list is
-- expected to grow; adding a site is then one INSERT migration, matching how 0007 seeds curation rules
-- and 0036 seeds the genre vocabulary. It deliberately starts small.
--
-- The handle CHECK mirrors PSN's own online-id rules (3-16 chars, alphanumeric plus - and _), which is
-- also what these sites key their profiles on. It is a defence-in-depth floor, not the only validation:
-- curator.profile_routes validates before insert so a caller gets a 400 rather than a 23514.

CREATE TABLE profile_link_sites
(
    site_key     TEXT PRIMARY KEY,
    display_name TEXT    NOT NULL,
    url_template TEXT    NOT NULL,
    sort_order   INTEGER NOT NULL,
    CONSTRAINT profile_link_sites_template_has_handle CHECK (url_template LIKE '%{handle}%')
);

INSERT INTO profile_link_sites (site_key, display_name, url_template, sort_order)
VALUES ('psnprofiles', 'PSNProfiles', 'https://psnprofiles.com/{handle}', 1),
       ('truetrophies', 'TrueTrophies', 'https://www.truetrophies.com/gamer/{handle}', 2),
       ('exophase', 'Exophase', 'https://www.exophase.com/psn/user/{handle}/', 3);

CREATE TABLE user_profile_links
(
    identity_sub UUID        NOT NULL REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    site_key     TEXT        NOT NULL REFERENCES profile_link_sites (site_key),
    handle       TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (identity_sub, site_key),
    CONSTRAINT user_profile_links_handle_check CHECK (handle ~ '^[A-Za-z0-9_-]{3,16}$')
);

CREATE INDEX user_profile_links_identity_sub_idx ON user_profile_links (identity_sub);
