-- Seeds size_estimates with the real install-size data ported in code (scoring/size_estimation_service.py)
-- from Tools/PlayStation/ps_sizes.py's get_install_size(), but never actually migrated into the table as
-- rows. Until this migration the table was empty, so estimate_install_size_gb() returned NULL for every
-- game; CollectionOrchestrator._score then fell through to its _DEFAULT_SIZE_GB = 20.0, so every
-- capacity_fill run packed every title at a uniform 20 GB regardless of its real size. The bin-pack did
-- not fail -- it produced a plausible-looking collection whose contents were decided by capacity/20.
--
-- Two axes, matching estimate_install_size_gb()'s resolution order: a per-title substring override wins
-- over the generic tier x genre-class band.

-- Tier x genre-class bands, from get_install_size()'s recalibrated 2026-07-05 heuristic. The legacy
-- function groups genres with OR ("open world" or "rpg"; "shooter" or "action"); one row per keyword
-- reproduces that, since estimate_install_size_gb() matches genre_class as a substring of the resolved
-- genre and no genre in the genres table contains two of these keywords.
--
-- 'open world' is seeded for fidelity with the legacy rule but is currently unreachable: genres holds
-- only single-token names (Shooter, RPG, Action, ...) and has no Open World entry, so no resolved genre
-- can match it. It costs nothing and becomes live if that vocabulary ever widens.
INSERT INTO size_estimates (title_pattern, aaa_tier, genre_class, platform, size_gb)
VALUES (NULL, 'AAA', 'open world', 'PS5', 81),
       (NULL, 'AAA', 'open world', 'PS4', 64),
       (NULL, 'AAA', 'rpg', 'PS5', 81),
       (NULL, 'AAA', 'rpg', 'PS4', 64),
       (NULL, 'AAA', 'shooter', 'PS5', 68),
       (NULL, 'AAA', 'shooter', 'PS4', 51),
       (NULL, 'AAA', 'action', 'PS5', 68),
       (NULL, 'AAA', 'action', 'PS4', 51),
       (NULL, 'AAA', NULL, 'PS5', 59),
       (NULL, 'AAA', NULL, 'PS4', 47),
       (NULL, 'AA', NULL, 'PS5', 18),
       (NULL, 'AA', NULL, 'PS4', 12),
       (NULL, 'Indie', NULL, 'PS5', 16),
       (NULL, 'Indie', NULL, 'PS4', 16);

-- Per-title overrides, from get_install_size()'s KNOWN_SIZES. That dict carries one size per title with
-- no platform axis, so each entry seeds both platforms with the same value -- the cross join states that
-- rather than duplicating every literal.
INSERT INTO size_estimates (title_pattern, aaa_tier, genre_class, platform, size_gb)
SELECT known.pattern, NULL, NULL, platforms.platform, known.size_gb
FROM (VALUES ('call of duty: modern warfare ii', 150),
             ('call of duty: modern warfare iii', 150),
             ('call of duty: modern warfare', 200),
             ('call of duty: warzone', 100),
             ('call of duty: vanguard', 90),
             ('call of duty: black ops 6', 100),
             ('call of duty: infinite warfare', 70),
             ('call of duty: ghosts', 50),
             ('call of duty: advanced warfare', 55),
             ('call of duty: wwii', 60),
             ('red dead redemption 2', 150),
             ('red dead redemption', 50),
             ('final fantasy vii rebirth', 150),
             ('final fantasy xv', 60),
             ('final fantasy xiv', 80),
             ('god of war', 50),
             ('horizon forbidden west', 95),
             ('horizon zero dawn', 50),
             ('marvel''s spider-man 2', 100),
             ('marvel''s spider-man remastered', 75),
             ('marvel''s spider-man', 50),
             ('the last of us part i', 80),
             ('the last of us part ii remastered', 80),
             ('the last of us part ii', 78),
             ('uncharted 4', 55),
             ('days gone', 60),
             ('returnal', 60),
             ('ratchet & clank', 42),
             ('astro''s playroom', 10),
             ('sackboy', 20),
             ('assassin''s creed odyssey', 65),
             ('assassin''s creed origins', 55),
             ('assassin''s creed mirage', 35),
             ('assassin''s creed shadows', 60),
             ('far cry 5', 45),
             ('rainbow six siege', 50),
             ('watch dogs legion', 50),
             ('starfield', 125),
             ('the elder scrolls online', 80),
             ('the witcher 3', 50),
             ('elden ring', 60),
             ('sekiro', 16),
             ('dark souls remastered', 8),
             ('bloodborne', 30),
             ('star wars jedi: survivor', 130),
             ('star wars jedi: fallen order', 55),
             ('star wars: squadrons', 20),
             ('battlefield 2042', 100),
             ('battlefield v', 90),
             ('fifa 23', 50),
             ('fifa 21', 50),
             ('borderlands: the handsome collection', 45),
             ('bioshock: the collection', 30),
             ('mafia: definitive edition', 35),
             ('mafia trilogy', 55),
             ('baldur''s gate 3', 150),
             ('batman: arkham knight', 45),
             ('mortal kombat 11', 55),
             ('hogwarts legacy', 75),
             ('crash bandicoot n. sane trilogy', 15),
             ('resident evil 4', 67),
             ('resident evil village', 35),
             ('monster hunter world', 22),
             ('monster hunter rise', 10),
             ('persona 3 reload', 35),
             ('persona 5 royal', 20),
             ('persona 5', 20)) AS known (pattern, size_gb)
         CROSS JOIN (VALUES ('PS5'), ('PS4')) AS platforms (platform);
