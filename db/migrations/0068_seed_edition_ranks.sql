-- Curator schema — migration 0068 (seed edition_ranks from the storefront's own edition ladder)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- edition_ranks was created in 0001 and never seeded, so the canonicalization edition ladder in the
-- ingestion runtime (EditionSortKey: active first, then PSGD over PS4GD, then the lowest rank whose keyword
-- the entitlement name contains, 99 when none does) was inert: every edition ranked 99 and the winning
-- name of a title owned in two editions was arbitrary.
--
-- The keywords and their order come from a captured metGetPricingDataByConceptId walk over one PS4
-- category page (recording under Tools/OpenAPI/PlayStation Catalog/recordings): the storefront's own
-- edition.type puts STANDARD below PREMIUM, and its invariantName vocabulary is the keyword source.
-- Rank 0 is the base edition, 1 an upgraded edition of the same game, 2 a bundle or pack of several. A
-- lower rank wins, so a title owned as both "Standard Edition" and "Digital Deluxe Edition" canonicalizes
-- under the base name. Seeded by migration, never at runtime, so a rebuilt database is byte-identical.

INSERT INTO edition_ranks (keyword, rank) VALUES
    ('standard edition', 0),
    ('deluxe edition', 1),
    ('definitive edition', 1),
    ('ultimate edition', 1),
    ('complete edition', 1),
    ('legendary edition', 1),
    ('bundle', 2),
    ('pack', 2);
