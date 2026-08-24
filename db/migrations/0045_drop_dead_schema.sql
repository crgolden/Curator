-- Curator schema — migration 0045 (drop four never-populated schema objects)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Four objects declared in 0001_initial.sql have never had a writer and have never held a value. Each is
-- zero-referenced across every repo in the fleet and zero-populated in production; carrying them costs a
-- reader of the schema a wrong guess about what the product does.
--
-- psn_game_search_cache / psn_player_search_cache: 0010's header already recorded that neither has an
-- INSERT anywhere. Since then psn_game_search_cache lost even its would-be writer -- PSN game search went
-- to the storefront gateway (curator.psn.store_client) and was never ported -- and
-- psn_player_search_cache's would-be writer, SocialClient.search_players, has no route and no internal
-- caller. The method itself stays: §16 set the precedent that a dead-but-tested client method
-- (collection_follower_count, is_following_collection) is a different disposition from a dead cache table.
-- A table nothing can write is not a cache, it is a claim about a feature that does not exist.
--
-- games.search_names: an alternate-title array for the merge pass that was never written to and never
-- read. Merging is MergeService.MergeByProductIdAndName's normalized_title plus product-id dual signal,
-- which is a different mechanism entirely.
--
-- game_enrichment.collection_tier: a five-value editorial ranking with no producer. It is deliberately
-- NOT wired up -- PARKING_LOT §7's settled decisions exclude it from the public catalog, because shipping
-- a permanently-null field to anonymous consumers is worse than not shipping the field. Its CHECK is an
-- inline column constraint (0001_initial.sql:233) and goes with the column, so no separate DROP
-- CONSTRAINT is needed. aaa_tier, immediately above it, is the tier the catalog actually publishes.
--
-- Irreversible, and that is the point -- but the evidence is a measurement, not an assumption. Confirm
-- immediately before the deploy that both tables still hold 0 rows and that search_names / collection_tier
-- still hold nothing; a single count taken days earlier against a mutable table is a timestamp, not a fact.
--
-- Measure search_names with cardinality(search_names) > 0, NOT with IS NOT NULL. The column is
-- TEXT[] NOT NULL DEFAULT '{}' (0001_initial.sql:168), so IS NOT NULL matches every row in games and can
-- never reach zero -- it reports the table's row count and reads as if real data were about to be lost.
-- collection_tier is nullable, so IS NOT NULL is correct for that one.
--
--   SELECT (SELECT count(*) FROM psn_game_search_cache)                 AS game_search_rows,
--          (SELECT count(*) FROM psn_player_search_cache)               AS player_search_rows,
--          (SELECT count(*) FROM games
--            WHERE cardinality(search_names) > 0)                       AS search_names_populated,
--          (SELECT count(*) FROM game_enrichment
--            WHERE collection_tier IS NOT NULL)                         AS collection_tier_set;
--
-- Verified all four = 0 against production on 2026-08-24 (1171 games, 1171 game_enrichment rows).

DROP TABLE psn_game_search_cache;

DROP TABLE psn_player_search_cache;

ALTER TABLE games
    DROP COLUMN search_names;

ALTER TABLE game_enrichment
    DROP COLUMN collection_tier;
