-- Widens the platform model past PS4/PS5 (AGENTS/DESIGNS.md §7 item 9).
--
-- Three tables carried CHECK (platform IN ('PS5','PS4')) and library_entries carried a boolean pair.
-- Two booleans cannot express N platforms, and 354 of 1047 rows are both PS4 and PS5, so ownership is a
-- set rather than a scalar.
--
-- Platform ids keep the existing uppercase form. That is already the vocabulary of every stored value,
-- every route contract these tables are written through, and the size/measured-size lookups. PSN's own
-- lowercase platformId values are a foreign vocabulary normalised at ingestion, not here; lowercasing the
-- database instead would have broken four call sites and a public request contract to gain nothing.

CREATE TABLE platforms
(
    platform_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true
);

INSERT INTO platforms (platform_id, name, sort_order) VALUES
    ('PS5',    'PlayStation 5',        0),
    ('PS4',    'PlayStation 4',        1),
    ('PS3',    'PlayStation 3',        2),
    ('PSVITA', 'PlayStation Vita',     3),
    ('PSP',    'PlayStation Portable', 4),
    ('PS2',    'PlayStation 2',        5),
    ('PS1',    'PlayStation',          6);

ALTER TABLE game_measured_sizes DROP CONSTRAINT game_measured_sizes_platform_check;
ALTER TABLE game_measured_sizes
    ADD CONSTRAINT game_measured_sizes_platform_fkey FOREIGN KEY (platform) REFERENCES platforms (platform_id);

ALTER TABLE size_estimates DROP CONSTRAINT size_estimates_platform_check;
ALTER TABLE size_estimates
    ADD CONSTRAINT size_estimates_platform_fkey FOREIGN KEY (platform) REFERENCES platforms (platform_id);

ALTER TABLE user_consoles DROP CONSTRAINT user_consoles_platform_check;
ALTER TABLE user_consoles
    ADD CONSTRAINT user_consoles_platform_fkey FOREIGN KEY (platform) REFERENCES platforms (platform_id);

-- opencritic_pagination_cursor.platform is deliberately left alone: its values are OpenCritic's own
-- platform slugs, not a console a user owns, and it keys an external API's pagination.

CREATE TABLE library_entry_platforms
(
    identity_sub UUID NOT NULL,
    game_id      UUID NOT NULL,
    platform     TEXT NOT NULL REFERENCES platforms (platform_id),
    PRIMARY KEY (identity_sub, game_id, platform),
    FOREIGN KEY (identity_sub, game_id) REFERENCES library_entries (identity_sub, game_id) ON DELETE CASCADE
);

CREATE INDEX idx_library_entry_platforms_game_id ON library_entry_platforms (game_id);
CREATE INDEX idx_library_entry_platforms_platform ON library_entry_platforms (platform);

INSERT INTO library_entry_platforms (identity_sub, game_id, platform)
SELECT identity_sub, game_id, 'PS5' FROM library_entries WHERE native_ps5;

INSERT INTO library_entry_platforms (identity_sub, game_id, platform)
SELECT identity_sub, game_id, 'PS4' FROM library_entries WHERE ps4_eligible;
