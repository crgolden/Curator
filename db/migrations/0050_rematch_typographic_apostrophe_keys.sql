-- Curator schema — migration 0050 (re-match the rows keyed under the pre-fix apostrophe rule)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- RawgMatcher.Normalize and OpenCriticNameIndex.Normalize both fold U+2019/U+2018/U+02BC/U+0060 onto the
-- ASCII apostrophe now; before the fix they did not, so the PS Store's typographic spelling and a provider
-- catalog's ASCII spelling produced different keys and never met. Both normalizers are fixed and pinned by
-- tests. Neither fix rewrites what the old rule already wrote, which is what this migration is for.
--
-- Exactly one normalized key is persisted anywhere: rawg_cache.normalized_title, written and read through
-- RawgMatcher.Normalize (EnrichmentRepository.cs:41 and :65). OpenCritic persists no key at all --
-- opencritic_cache is (oc_game_id, name, score, tier, percent_recommended, raw, fetched_at) and the name
-- index is built in memory per process from those rows (EnrichmentOrchestrationService.cs:299, discarded at
-- :344), so it already rebuilds under the fixed rule and has nothing to repair here.
--
-- Staleness is defined as "not a fixed point of the current normalizer" rather than "contains a curly
-- quote". Applying the current rule to an already-normalized key is a no-op on every correct row and
-- changes exactly the rows some older rule wrote, whatever that rule was -- so this needs no knowledge of
-- the old regex, and it is self-limiting by construction: after this runs every key is a fixed point and a
-- re-run matches nothing. That is a stronger idempotence claim than 0048's, and unlike 0048 it survives the
-- pass that follows.
--
-- rawg_normalized_key reproduces RawgMatcher.Normalize (RawgMatcher.cs:18-25) step for step: strip [™®©],
-- fold the four apostrophe forms onto U+0027, fold en/em dash onto the hyphen, collapse whitespace, trim,
-- lowercase. It is created and dropped inside this migration so the character classes exist once here
-- instead of once per reference, and so no second, drifting copy of a C# rule is left behind in the schema.
-- The classes are written as U& escapes because the glyphs are indistinguishable in a terminal and a
-- reviewer cannot check a pasted one. It is only reproducible because RawgMatcher does no NFKD folding and
-- no roman numeral mapping; OpenCriticNameIndex.Normalize does both and is deliberately not reimplemented.
--
-- The second statement is the one that actually re-matches. A game with a game_enrichment row and a stamped
-- rawg_attempted_at is never revisited: the worklist is "no row" UNION "rawg_attempted_at IS NULL"
-- (EnrichmentRunProcessor.cs:122-125). Clearing the stamp is 0043's own mechanism for "ask again", and
-- because EnrichGameAsync resolves OpenCritic in the same call (EnrichmentOrchestrationService.cs:108) it
-- re-runs both matchers, not just RAWG. That is why no new bookkeeping column is added for OpenCritic.
--
-- 0043's header warns (0043 lines 33-38) that putting a rawg_enriched = true row back into the retry set
-- risks a pass running during a RAWG outage overwriting its good values with the NULLs it got. The writer
-- is EnrichmentRepository.SaveGameEnrichmentAsync, and its ON CONFLICT DO UPDATE guards exactly five
-- columns against a pass that did not consult the provider: developer, critical_score and score_source
-- fall back unless @rawg_attempted, and psn_rating/psn_enriched unless @psn_enriched. All nine rows this
-- clears locally are rawg_enriched = true, so those guards are load-bearing here rather than incidental.
--
-- Every OTHER column on that statement -- genre_id, subgenre_id, release_year, publisher, esrb,
-- multiplayer, oc_score, oc_tier, oc_percent_recommended -- is still a bare EXCLUDED overwrite. A pass
-- that re-enrols one of these rows and answers with NULLs replaces whatever was there. That is the
-- accepted behaviour for provider-owned fields, not an oversight, but do not read the paragraph above as
-- covering them.
--
-- Measured on the local database immediately before this ran: rawg_cache 973 rows, 9 not a fixed point (all
-- 9 positives, 9 distinct fresh keys, 0 already taken); games 1378 rows, 13 carrying a typographic
-- apostrophe; game_enrichment 1179 rows, 9 tombstoned with a provider miss on one of those 13. Production
-- is expected to differ and is deliberately not measured from here.
--
-- The one limit worth stating plainly: statements 1 and 3 are no-ops on a second application forever, but
-- clearing rawg_attempted_at is a no-op only until the next pass re-stamps it, after which a re-application
-- would clear it again. run_migrations.py records applied filenames (run_migrations.py:28-38) so it is
-- applied once, and it is a no-op on a fresh database such as CI's service container -- but it is not
-- self-limiting the way the other two are, and claiming otherwise would borrow a guarantee it lacks.

CREATE FUNCTION rawg_normalized_key(title TEXT) RETURNS TEXT
    LANGUAGE sql
    IMMUTABLE
    STRICT
AS
$$
SELECT lower(btrim(regexp_replace(
    translate(
        translate(
            translate(title, U&'\2122\00AE\00A9', ''),
            U&'\2019\2018\02BC\0060', U&'\0027\0027\0027\0027'),
        U&'\2013\2014', '--'),
    '\s+', ' ', 'g')))
$$;

-- Copy each stale row to the key the current normalizer produces for it. DISTINCT ON is what keeps this
-- from aborting the deploy: the old rule preserved each typographic form as its own key, so one title can
-- sit in this table under both U+2019 and U+2018 and both now fold to the same fresh key. Two bare UPDATEs
-- would each see the other's key as free and collide on the primary key (0001_initial.sql:246), failing the
-- whole file -- and because a failed file is never recorded in schema_migrations, every later deploy would
-- retry and fail identically with no way to self-heal. Electing one winner per fresh key removes that
-- possibility rather than betting the table never contains such a pair.
--
-- The winner is the one carrying a real payload, then the most recently fetched, then the key itself so the
-- result never depends on scan order. The same preference decides a collision with a row already sitting on
-- the fresh key: ON CONFLICT overwrites only a cached miss, and only with a real payload. RAWG is searched
-- with the raw title (EnrichmentOrchestrationService.cs:213), so a miss recorded for the ASCII spelling was
-- never evidence about the typographic one -- yet after the fix the typographic title normalizes onto that
-- key and would be served that miss at :203-208. A real payload therefore outranks a cached nothing, and
-- two rows that agree leave the incumbent alone.
INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw, fetched_at)
SELECT DISTINCT ON (rawg_normalized_key(normalized_title)) rawg_normalized_key(normalized_title),
                                                           rawg_game_id,
                                                           raw,
                                                           fetched_at
FROM rawg_cache
WHERE normalized_title <> rawg_normalized_key(normalized_title)
ORDER BY rawg_normalized_key(normalized_title), (raw IS NOT NULL) DESC, fetched_at DESC, normalized_title
ON CONFLICT (normalized_title) DO UPDATE SET rawg_game_id = EXCLUDED.rawg_game_id,
                                             raw = EXCLUDED.raw,
                                             fetched_at = EXCLUDED.fetched_at
WHERE rawg_cache.raw IS NULL
  AND EXCLUDED.raw IS NOT NULL;

-- The originals have been copied to their fresh key, and any row still not a fixed point lost the election
-- above. Either way it is unreachable under the current normalizer, so leaving it is leaving a row nothing
-- can ever read. A loser that was a cached miss is the right thing to lose: RAWG is simply asked again.
DELETE
FROM rawg_cache stale
WHERE stale.normalized_title <> rawg_normalized_key(stale.normalized_title);

-- Re-enrol the games the old rule mismatched. Scoped to a provider that actually missed, so a title that
-- both providers already matched is not re-queried for nothing.
UPDATE game_enrichment
SET rawg_attempted_at = NULL
WHERE rawg_attempted_at IS NOT NULL
  AND (rawg_enriched = false OR opencritic_enriched = false)
  AND game_id IN (SELECT game_id
                  FROM games
                  WHERE canonical_title ~ ('[' || U&'\2019\2018\02BC\0060' || ']'));

DROP FUNCTION rawg_normalized_key(TEXT);
