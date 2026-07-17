-- Curator schema — migration 0002 (PSN data-harvest preferences)
-- Target: PostgreSQL 17. Applied manually via psql (see TESTING.md) — there is no migration runner.
--
-- Adds four opt-in-by-default boolean flags to psn_links, letting each user control which categories of
-- PSN data Curator is allowed to harvest/display for them: trophies, identity (PSN account id/online id
-- lookups), presence, and devices. Every flag defaults to false — a newly linked user grants nothing until
-- they explicitly opt in — and enforcement happens server-side, at the route layer, not merely in the UI.
--
-- These flags live on psn_links (one row per linked user already exists, from migration 0001) rather than
-- a new preferences table, so every gated route can check a flag off the same row it already reads for the
-- link itself, with no extra join. This is a deliberate narrow exception to 0001's "no Postgres tables for
-- trophy data, presence, the social graph, devices, or chat reads" rule: these columns are account
-- metadata about *what Curator may read* from PSN on the user's behalf, never the harvested PSN data
-- itself, which continues to live only in Redis (trophies) or stay live-proxy-only (presence/devices).

ALTER TABLE psn_links
    ADD COLUMN harvest_trophies BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN harvest_identity BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN harvest_presence BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN harvest_devices BOOLEAN NOT NULL DEFAULT false;
