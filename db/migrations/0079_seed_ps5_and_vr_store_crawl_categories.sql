-- Curator schema — migration 0079 (seed the PS5 and PSVR categories the storefront crawl walks)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Each row's id and reporting_name_prefix are read from the live storefront, and each category is verified to
-- return products rather than only a totalCount:
--   PS5  4cbf39e2-5749-4970-ba81-93a489e4570c  WM_EU_PS5_ALL_GAMES  9320 products, FULL_GAME facet 7005
--   PSVR 95239ca7-2dcf-43d9-8d4b-b7672ee9304a  GMA_PSVR_DYNAMIC      547 products, FULL_GAME facet  475
--
-- Together with 0078's PS4 category these are the storefront's walkable platform grids. The remaining ids in
-- the PHP wrapper's CategoryEnum are not walkable through categoryGridRetrieve: ALL_CONCEPTS, VR2,
-- FREE_GAMES and NEW_GAMES each report a totalCount and return no products at any offset, and PS_PLUS and
-- SALES report zero. A category that answers with a name is not a category that can be walked, so a new row
-- is added only after a probe that saw products come back.
--
-- The PS Plus Game Catalog and Classics Catalog have their own walk (ps_plus_catalog_categories) and are
-- deliberately absent here: walking them twice would admit the same games through two paths.

INSERT INTO store_crawl_categories (category_id, platform, reporting_name_prefix)
VALUES ('4cbf39e2-5749-4970-ba81-93a489e4570c', 'ps5', 'WM_EU_PS5_ALL_GAMES'),
       ('95239ca7-2dcf-43d9-8d4b-b7672ee9304a', 'psvr', 'GMA_PSVR_DYNAMIC')
ON CONFLICT (category_id) DO NOTHING;
