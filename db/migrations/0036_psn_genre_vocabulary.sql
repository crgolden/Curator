-- Replaces the genres table's contents with PlayStation Store's own controlled vocabulary.
--
-- PS Store is the source of truth for every field it publishes, and that now extends to the reference
-- table itself: the 23 rows below are exactly the `productGenres` facet returned by categoryGridRetrieve,
-- which enumerates the whole vocabulary with counts in one anonymous call. Seeding from a migration rather
-- than a runtime sync keeps a rebuilt database byte-identical and independent of PSN being reachable;
-- drift is meant to be detected and turned into the next migration, never auto-applied.
--
-- The previous contents came from Tools/PlayStation/ps_genre.py's GENRE_PRIORITY list, which was RAWG's
-- vocabulary. Several of those rows exist nowhere in PSN's (Platformer, MOBA, Board Games, Card, Indie),
-- and it carried spelling variants of one another (Sport/Sports, Driving/Racing/Racing) that fragmented a
-- single genre across two foreign keys depending on which source spelled it which way.
--
-- `name` holds PSN's key (stable and locale-independent); `display_name` holds the human label. Matching
-- lowercases both sides, so PSN's ROLE_PLAYING_GAMES resolves directly, and a RAWG tag resolves only when
-- it happens to coincide with a PSN key (Action, Adventure, Shooter, Racing, Sports, ...). RAWG-only
-- spellings no longer resolve at all, which is the intended consequence of PSN being the sole vocabulary.
--
-- `priority` is deliberately NOT sourced from PSN. PSN publishes names and counts but no specificity
-- ranking, and pick_genre_subgenre needs one to choose a primary genre over a subgenre. The ordering below
-- is Curator's editorial judgment, preserving the legacy list's philosophy: concrete gameplay genres rank
-- ahead of generic audience/style descriptors, so ACTION and CASUAL lose to SHOOTER and RACING. Facet
-- counts are explicitly not used -- ACTION being the most common makes it the least specific, not the most.

ALTER TABLE genres
    ADD COLUMN display_name TEXT;

UPDATE game_enrichment
SET genre_id    = NULL,
    subgenre_id = NULL;

DELETE FROM genres;

INSERT INTO genres (name, display_name, priority, active)
VALUES ('SHOOTER', 'Shooter', 0, true),
       ('FIGHTING', 'Fighting', 1, true),
       ('ROLE_PLAYING_GAMES', 'Role Playing Games', 2, true),
       ('SPORTS', 'Sports', 3, true),
       ('RACING', 'Racing', 4, true),
       ('PUZZLE', 'Puzzle', 5, true),
       ('MUSIC/RHYTHM', 'Music/Rhythm', 6, true),
       ('HORROR', 'Horror', 7, true),
       ('STRATEGY', 'Strategy', 8, true),
       ('QUIZ', 'Quiz', 9, true),
       ('BRAIN_TRAINING', 'Brain Training', 10, true),
       ('EDUCATIONAL', 'Educational', 11, true),
       ('FITNESS', 'Fitness', 12, true),
       ('PARTY', 'Party', 13, true),
       ('ADULT', 'Adult', 14, true),
       ('SIMULATOR', 'Simulator', 15, true),
       ('SIMULATION', 'Simulation', 16, true),
       ('ARCADE', 'Arcade', 17, true),
       ('ADVENTURE', 'Adventure', 18, true),
       ('FAMILY', 'Family', 19, true),
       ('CASUAL', 'Casual', 20, true),
       ('ACTION', 'Action', 21, true),
       ('UNIQUE', 'Unique', 22, true);

ALTER TABLE genres
    ALTER COLUMN display_name SET NOT NULL;

UPDATE collection_definitions
SET genre_filter = ARRAY(SELECT upper(replace(tag, ' ', '_')) FROM unnest(genre_filter) AS tag)
WHERE genre_filter IS NOT NULL
  AND cardinality(genre_filter) > 0;
