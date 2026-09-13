-- Curator schema — migration 0063 (download sizes from the web-store entitlements, and media ceilings)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- size_estimates covers PS4 and PS5 only, and game_measured_sizes has produced nothing, so every PS3, Vita
-- and PSP title packs at the flat default. Sony's own web-store entitlements endpoint reports each package's
-- contentSize per drmContents entry, official and per user; that is the source, and the only one in scope.
-- The download sizes live in their own table, separate from game_measured_sizes (human, last-writer-wins),
-- so a machine source never overwrites a person's measurement.
--
-- platforms.media_ceiling_gb is the physical media ceiling a title cannot exceed on that platform: a fact
-- fixed outside the repo (UMD 1.8 GB, Vita card 4 GB, dual-layer Blu-ray 50 GB). NULL for PS4 and PS5,
-- whose titles routinely exceed the default. It is the rung below the estimate and above the flat default.

ALTER TABLE platforms
    ADD COLUMN media_ceiling_gb NUMERIC(6, 2);

UPDATE platforms SET media_ceiling_gb = 1.80 WHERE platform_id = 'PSP';
UPDATE platforms SET media_ceiling_gb = 4.00 WHERE platform_id = 'PSVITA';
UPDATE platforms SET media_ceiling_gb = 50.00 WHERE platform_id = 'PS3';

CREATE TABLE game_download_sizes (
    game_id    UUID NOT NULL REFERENCES games (game_id) ON DELETE CASCADE,
    platform   TEXT NOT NULL REFERENCES platforms (platform_id),
    bytes      BIGINT NOT NULL CHECK (bytes > 0),
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (game_id, platform)
);
