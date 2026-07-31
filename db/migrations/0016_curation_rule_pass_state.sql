-- Curator schema — migration 0016 (curation rule pass state)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Tracks the content fingerprint of franchise_rules/publisher_tiers as of the last time
-- POST /enrichment/runs actually ran CatalogRepository.reclassify_franchise / EnrichmentRepository
-- .reclassify_tier -- so a re-run can skip a full-catalog pass when the driving rule set hasn't
-- changed. Neither rule table has any application-level INSERT/UPDATE path (both are edited by
-- migration or ad-hoc SQL), so there is nothing to hook a trigger or updated_at column onto -- this
-- small table is state the handler itself owns and updates.

CREATE TABLE curation_rule_pass_state
(
    pass_name         TEXT PRIMARY KEY CHECK (pass_name IN ('franchise_reclassification', 'tier_reclassification')),
    rules_fingerprint TEXT NOT NULL,
    last_ran_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
