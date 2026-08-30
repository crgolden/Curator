-- Curator schema — migration 0047 (keep the storefront payload the catalog walk already pays for)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- StoreCatalogClient._to_product projected six fields out of each product node and dropped the rest:
-- price (basePrice, discountedPrice, isFree, isTiedToSubscription, serviceBranding), sortingOptions, skus
-- and the sibling concepts collection. Nothing about that data needs a new operation or a second request —
-- it arrives inside the categoryGridRetrieve response POST /catalog/backfill already makes. Discarding it
-- means recovering it later costs a whole second walk of a live, shifting collection.
--
-- Same remedy as 0010 applied to entitlement_snapshots: keep the response. A column per field would have to
-- guess a schema for a projection Sony changes without telling us, and a column that is always NULL is
-- worse than the discard; jsonb keeps the payload honest and lets a later migration promote whichever
-- fields turn out to earn a column.
--
-- DEFAULT '{}' rather than NULL, matching entitlement_snapshots.raw, so a reader never has to distinguish
-- "no payload stored" from "payload stored and empty" — both mean "this row predates a walk that kept it".
-- This does NOT backfill: rows written before this migration keep '{}' until a walk re-runs, and
-- backfill_store_products' ON CONFLICT refreshes them in place when it does.

ALTER TABLE psn_catalog_cache
    ADD COLUMN raw JSONB NOT NULL DEFAULT '{}'::jsonb;
