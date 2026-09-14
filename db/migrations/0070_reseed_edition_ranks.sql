-- Curator schema — migration 0070 (re-seed edition_ranks idempotently)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- 0068 seeds edition_ranks with a bare INSERT, which is correct on a fresh database and unreachable on any
-- database that recorded 0068's filename before that INSERT was in the file. One such database exists: the
-- local curator_test, where schema_migrations holds 0068_seed_edition_ranks.sql and edition_ranks holds
-- zero rows, so test_edition_ranks_are_seeded_with_the_base_edition_ranking_first fails locally and passes
-- in CI against a fresh service container. Editing 0068 would fix neither, because an applied filename is
-- never re-read.
--
-- Idempotent by predicate rather than by guard: ON CONFLICT DO NOTHING over the keyword primary key, so a
-- database 0068 already seeded is untouched and one it skipped is repaired. A rank that was deliberately
-- changed by hand is also left alone, which is the safer default for config-as-data nobody re-derives.

INSERT INTO edition_ranks (keyword, rank) VALUES
    ('standard edition', 0),
    ('deluxe edition', 1),
    ('definitive edition', 1),
    ('ultimate edition', 1),
    ('complete edition', 1),
    ('legendary edition', 1),
    ('bundle', 2),
    ('pack', 2)
ON CONFLICT (keyword) DO NOTHING;
