-- Curator schema — migration 0077 (the storefront crawl's categories and its resume cursor)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Functions' StoreCatalogCrawlWorker walks these categories a bounded number of pages per run and admits the
-- full games it finds, so the cursor has to outlive an invocation: POST /catalog/backfill round-trips its
-- next_offset through the HTTP response instead, which no timer can do. One row per category holds both the
-- configuration (which category, which platform, the reporting-name prefix that proves the id still names
-- what it named when it was seeded) and the walk state.
--
-- reporting_name_prefix follows ps_plus_catalog_categories (0058): Sony reuses category ids across
-- promotions, so a walk that finds a different reportingName on page one is walking a different set and
-- stops rather than admitting it.
--
-- No category is seeded here. A row needs two values that only a live storefront call can supply: the
-- category id (only the PS4 one, store_client.PS4_GAMES_CATEGORY_ID, is recorded in this workspace, and no
-- PS5 id is) and the reportingName that id currently answers with. Seeding a guessed prefix would arm the
-- guard with a value that is wrong in exactly the case the guard exists to catch, so the rows land in their
-- own migration once both are probed. Until then the crawl has nothing to walk and does nothing, which is
-- the intended inert arrival.

CREATE TABLE store_crawl_categories
(
    category_id           UUID PRIMARY KEY,
    platform              TEXT        NOT NULL,
    reporting_name_prefix TEXT        NOT NULL,
    next_offset           INTEGER     NOT NULL DEFAULT 0,
    walk_started_at       TIMESTAMPTZ,
    walk_completed_at     TIMESTAMPTZ,
    reported_total        INTEGER,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
