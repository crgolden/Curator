-- Curator schema — migration 0071 (one games row per title key, kept so by a unique index)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- games.normalized_title has had two writers computing it differently. The ingestion runtime keys a game
-- on the trimmed lower-case form of a name it has already compatibility-decomposed, stripped of combining
-- marks, trademark symbols and letters and empty parentheses, and whitespace-collapsed. Curator's two
-- storefront writers (backfill_store_products, admit_store_game) keyed on the lower-cased vendor string
-- alone, so "Bloodborne™" and "God of War Ragnarök" each became a second row beside the game a library
-- refresh had already admitted, and every read that keys on game_id (excludeOwned, the manual candidates,
-- collections) treated the pair as two games. Both writers now share curator.catalog.title_normalization,
-- which reproduces the ingestion runtime's rule; this migration repairs what the old rule wrote and makes
-- the schema refuse a repeat.
--
-- Staleness is "not a fixed point of the current rule", as in 0050: a correct row is unchanged by it and
-- a re-application matches nothing. game_title_display and game_title_key reproduce
-- CanonicalizationService.NormalizeName plus the trim-and-lower in CatalogRepository.UpsertGameAsync step
-- for step, using normalize(NFKD) for the decomposition and the Unicode combining-mark blocks for the
-- category the runtime filters on. Both functions are created and dropped inside this migration so no
-- second copy of the rule is left in the schema to drift.
--
-- Within a duplicate set the winner is the row a game_concepts row already points at (a refresh or a
-- verified Store hit reached it), then the row more libraries hold, then the oldest. Every referencing
-- row moves to the winner; a row the winner already has an equivalent of (the same library, collection,
-- console, device, platform or enrichment row) is dropped in favour of the winner's. Rows are copied
-- through jsonb_populate_record so no column list here can drift from the table it names, and the
-- library-entry children move before their parents are deleted because their foreign key cascades.
--
-- Measure before and after with the query the header of the validation run used:
--   SELECT game_title_key(canonical_title), count(*) FROM games GROUP BY 1 HAVING count(*) > 1;
-- The unique index is the guarantee; the index it replaces was non-unique and that is the whole defect.

CREATE FUNCTION game_title_display(title TEXT) RETURNS TEXT
    LANGUAGE sql
    IMMUTABLE
    STRICT
AS
$$
SELECT btrim(regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                regexp_replace(normalize(title, NFKD),
                               U&'[\0300-\036F\1AB0-\1AFF\1DC0-\1DFF\20D0-\20FF\FE20-\FE2F]', '', 'g'),
                U&'[\2122\00AE\00A9]', '', 'g'),
            'TM\M', '', 'g'),
        '\(\s*\)', '', 'g'),
    '\s+', ' ', 'g'))
$$;

CREATE FUNCTION game_title_key(title TEXT) RETURNS TEXT
    LANGUAGE sql
    IMMUTABLE
    STRICT
AS
$$
SELECT lower(game_title_display(title))
$$;

CREATE TEMP TABLE game_merges ON COMMIT DROP AS
WITH ranked AS (
    SELECT g.game_id,
           game_title_key(g.canonical_title) AS title_key,
           row_number() OVER (
               PARTITION BY game_title_key(g.canonical_title)
               ORDER BY EXISTS (SELECT 1 FROM game_concepts gc WHERE gc.game_id = g.game_id) DESC,
                        (SELECT count(*) FROM library_entries le WHERE le.game_id = g.game_id) DESC,
                        g.created_at,
                        g.game_id
           ) AS rank
    FROM games g
)
SELECT loser.game_id  AS loser_id,
       winner.game_id AS winner_id
FROM ranked loser
JOIN ranked winner ON winner.title_key = loser.title_key AND winner.rank = 1
WHERE loser.rank > 1;

UPDATE game_concepts gc
SET game_id = m.winner_id
FROM game_merges m
WHERE gc.game_id = m.loser_id;

UPDATE psn_catalog_cache pcc
SET game_id = m.winner_id
FROM game_merges m
WHERE pcc.game_id = m.loser_id;

INSERT INTO library_entries
SELECT (jsonb_populate_record(NULL::library_entries, to_jsonb(le) || jsonb_build_object('game_id', m.winner_id))).*
FROM library_entries le
JOIN game_merges m ON le.game_id = m.loser_id
ON CONFLICT DO NOTHING;

INSERT INTO library_entry_platforms
SELECT (jsonb_populate_record(NULL::library_entry_platforms, to_jsonb(lep) || jsonb_build_object('game_id', m.winner_id))).*
FROM library_entry_platforms lep
JOIN game_merges m ON lep.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM library_entries le
USING game_merges m
WHERE le.game_id = m.loser_id;

INSERT INTO library_exclusions
SELECT (jsonb_populate_record(NULL::library_exclusions, to_jsonb(lx) || jsonb_build_object('game_id', m.winner_id))).*
FROM library_exclusions lx
JOIN game_merges m ON lx.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM library_exclusions lx
USING game_merges m
WHERE lx.game_id = m.loser_id;

INSERT INTO collection_definition_items
SELECT (jsonb_populate_record(NULL::collection_definition_items, to_jsonb(cdi) || jsonb_build_object('game_id', m.winner_id))).*
FROM collection_definition_items cdi
JOIN game_merges m ON cdi.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM collection_definition_items cdi
USING game_merges m
WHERE cdi.game_id = m.loser_id;

INSERT INTO collection_items
SELECT (jsonb_populate_record(NULL::collection_items, to_jsonb(ci) || jsonb_build_object('game_id', m.winner_id))).*
FROM collection_items ci
JOIN game_merges m ON ci.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM collection_items ci
USING game_merges m
WHERE ci.game_id = m.loser_id;

INSERT INTO console_installs
SELECT (jsonb_populate_record(NULL::console_installs, to_jsonb(ci) || jsonb_build_object('game_id', m.winner_id))).*
FROM console_installs ci
JOIN game_merges m ON ci.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM console_installs ci
USING game_merges m
WHERE ci.game_id = m.loser_id;

INSERT INTO storage_device_installs
SELECT (jsonb_populate_record(NULL::storage_device_installs, to_jsonb(sdi) || jsonb_build_object('game_id', m.winner_id))).*
FROM storage_device_installs sdi
JOIN game_merges m ON sdi.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM storage_device_installs sdi
USING game_merges m
WHERE sdi.game_id = m.loser_id;

INSERT INTO game_measured_sizes
SELECT (jsonb_populate_record(NULL::game_measured_sizes, to_jsonb(gms) || jsonb_build_object('game_id', m.winner_id))).*
FROM game_measured_sizes gms
JOIN game_merges m ON gms.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM game_measured_sizes gms
USING game_merges m
WHERE gms.game_id = m.loser_id;

INSERT INTO game_download_sizes
SELECT (jsonb_populate_record(NULL::game_download_sizes, to_jsonb(gds) || jsonb_build_object('game_id', m.winner_id))).*
FROM game_download_sizes gds
JOIN game_merges m ON gds.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM game_download_sizes gds
USING game_merges m
WHERE gds.game_id = m.loser_id;

INSERT INTO game_enrichment
SELECT (jsonb_populate_record(NULL::game_enrichment, to_jsonb(ge) || jsonb_build_object('game_id', m.winner_id))).*
FROM game_enrichment ge
JOIN game_merges m ON ge.game_id = m.loser_id
ON CONFLICT DO NOTHING;

DELETE FROM game_enrichment ge
USING game_merges m
WHERE ge.game_id = m.loser_id;

DELETE FROM games g
USING game_merges m
WHERE g.game_id = m.loser_id;

UPDATE games
SET canonical_title  = game_title_display(canonical_title),
    normalized_title = game_title_key(canonical_title),
    updated_at       = now()
WHERE canonical_title <> game_title_display(canonical_title)
   OR normalized_title <> game_title_key(canonical_title);

DROP INDEX idx_games_normalized_title;

CREATE UNIQUE INDEX games_normalized_title_key ON games (normalized_title);

DROP FUNCTION game_title_key(TEXT);

DROP FUNCTION game_title_display(TEXT);
