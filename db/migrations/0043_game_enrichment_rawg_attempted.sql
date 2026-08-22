-- Curator schema — migration 0043 (distinguish "RAWG had nothing" from "RAWG was never asked")
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- game_enrichment.rawg_enriched is a two-valued answer to a three-valued question. It records whether
-- RAWG contributed anything, and false therefore conflates "RAWG answered and genuinely has no entry for
-- this title" with "RAWG was never reached". The catalog enrichment pass then treats the mere existence
-- of a game_enrichment row as proof the game is enriched, so a false written during a provider outage is
-- permanent: the game is never revisited.
--
-- That is not hypothetical. On 2026-08-13 a catalog-wide pass ran while RAWG was unreachable and wrote
-- 1167 rows with rawg_enriched = false and no developer, no critical_score and no score_source -- RAWG
-- succeeded 0 times out of 1167. Every run since has reported full coverage in well under a minute
-- because the predicate hands it roughly one game. rawg_cache corroborates it: 4 rows, all positive, all
-- from 2026-08-21, so the 2026-08-13 pass never once got far enough to record even a negative result.
--
-- rawg_attempted_at supplies the missing third value. It is stamped only when RAWG actually answered --
-- a fresh search that returned, or a cached answer including a cached miss -- and is deliberately NOT
-- stamped when RAWG had no credential, was already marked transport-unavailable, or failed transport.
-- The pair then reads:
--
--   rawg_enriched = true                            -> RAWG answered and had the title
--   rawg_enriched = false, rawg_attempted_at NOT NULL -> RAWG answered and genuinely lacks the title
--   rawg_enriched = false, rawg_attempted_at NULL     -> RAWG was never reached; retry is warranted
--
-- Nullable with no default and no backfill, which is what repairs the incident rather than papering over
-- it: every pre-existing row becomes "never reached", so the 1167 become eligible for a backfill while a
-- genuine RAWG miss recorded from here on is left alone. Backfilling now() instead would tombstone the
-- entire catalog permanently and reproduce the original defect at full scale.

ALTER TABLE game_enrichment
    ADD COLUMN rawg_attempted_at TIMESTAMPTZ;

-- The one backfill that IS truthful, and it is load-bearing. rawg_enriched = true can only have been
-- written by a pass where RAWG answered and had the title, so stamping those rows asserts nothing that
-- did not happen. It also keeps the handful of genuinely-good rows out of the retry set: without it every
-- row in the table is "never asked", so the four titles that do carry a RAWG-sourced developer and
-- critical_score would be re-enriched, and a pass running while RAWG is down would overwrite them with
-- the NULLs it got. enriched_at is the closest honest timestamp -- it is when that answer was recorded.
UPDATE game_enrichment
SET rawg_attempted_at = enriched_at
WHERE rawg_enriched = true;

CREATE INDEX game_enrichment_rawg_unattempted_idx
    ON game_enrichment (game_id)
    WHERE rawg_attempted_at IS NULL;
