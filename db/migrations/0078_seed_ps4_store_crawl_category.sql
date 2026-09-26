-- Curator schema — migration 0078 (seed the PS4 category the storefront crawl walks)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Both values are read from the live storefront rather than assumed: the category answers categoryGridRetrieve
-- with reportingName WM_EU_ALL_PS4_GAMES, which is the prefix the crawl's rename guard compares page one
-- against, and it reports 7,340 products under storeDisplayClassification:FULL_GAME. Sony reuses category ids
-- across promotions, so a walk that finds a different reportingName is walking a different set and stops.
--
-- The PS5 and PSVR grids are seeded by 0079, which carries their probe evidence and the list of containers
-- that answer with a name and a total while returning no products, so they are never seeded at all.

INSERT INTO store_crawl_categories (category_id, platform, reporting_name_prefix)
VALUES ('44d8bb20-653e-431e-8ad0-c0a365f68d2f', 'ps4', 'WM_EU_ALL_PS4_GAMES')
ON CONFLICT (category_id) DO NOTHING;
