-- Curator schema — migration 0051 (BOARD_GAMES joins the genre vocabulary)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- 0036 seeded `genres` as exactly PlayStation Store's `productGenres` facet and recorded that Board Games
-- was dropped because it belonged to RAWG's vocabulary and not PSN's. That is no longer true: the live
-- facet on category 44d8bb20-653e-431e-8ad0-c0a365f68d2f now publishes 24 keys, and BOARD_GAMES is the
-- 24th (3 products). Every one of the 23 seeded keys is still published, so this is a pure addition.
--
-- This is the drift path 0036's header describes -- detected and turned into a migration, never
-- auto-applied -- rather than a re-introduction of a RAWG genre. The distinction matters because the
-- standing rule is that PS Store is the source of truth and its values are not massaged: BOARD_GAMES is
-- here because PSN publishes it, and it would be removed again the day PSN stops.
--
-- Priority is Curator's editorial judgment and is deliberately NOT sourced from PSN, which publishes no
-- specificity ranking. BOARD_GAMES takes 8, immediately ahead of STRATEGY, because the seed's stated
-- philosophy is that the more specific key wins the primary slot: a digital board game is almost always
-- also tagged STRATEGY, and naming the concrete form is more useful than the broader one. Everything
-- from STRATEGY down shifts by one. `priority` carries no UNIQUE constraint, so the shift is a single
-- statement with no ordering hazard, but leaving a tie would make pick_genre_subgenre's choice between
-- two equally ranked genres depend on row order.
--
-- No backfill. Nothing in game_enrichment can already reference this genre, and re-running enrichment is
-- what assigns it -- inventing a genre for an existing row would be asserting a fact PSN never gave us.

UPDATE genres
SET priority = priority + 1
WHERE priority >= 8;

INSERT INTO genres (name, display_name, priority, active)
VALUES ('BOARD_GAMES', 'Board Games', 8, true);
