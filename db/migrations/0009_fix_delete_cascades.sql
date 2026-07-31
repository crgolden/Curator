-- Curator schema — migration 0009 (fix DELETE /me cascades)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Fixes a live bug: DELETE /me (curator.me_routes) fails with a foreign-key violation for any user who
-- has ingested entitlements or saved a collection. curator.persistence.repository.Repository.delete_user
-- deletes the app_users row and relies on ON DELETE CASCADE to clear every per-user table, but eight of
-- the twelve per-user foreign keys created in 0001_initial.sql were declared without a cascade clause.
-- Only psn_links, psn_test_accounts (0001), user_enrichment_keys (0004), user_profiles (0005) and
-- follows (0006) ever had one.
--
-- Four child-chain foreign keys are fixed here too. They point at per-user parents rather than at
-- app_users directly, so the delete would still have failed at the child even once the eight parents
-- cascaded: entitlement_snapshots -> entitlement_pulls, collection_items -> collection_runs,
-- console_installs -> user_consoles, and collection_runs -> collection_definitions. The last of these
-- also gives DELETE /collections/{id} (added later) the run-history cleanup it needs; collection_runs
-- .definition_id stays nullable because a preview/inline run legitimately has no definition.
--
-- DELIBERATELY NOT CHANGED: account_action_log has no foreign key to app_users at all, by design. It
-- must survive account deletion for its retention window (GDPR Art. 17(3)(e) — erasure does not override
-- retention needed to defend legal claims). See 0003_account_action_log.sql. Do not add one.
--
-- Constraint names below are PostgreSQL's auto-generated <table>_<column>_fkey form, which is what
-- 0001_initial.sql's inline REFERENCES clauses produced.

-- Per-user tables referencing app_users directly.

ALTER TABLE entitlement_pulls
    DROP CONSTRAINT entitlement_pulls_identity_sub_fkey;
ALTER TABLE entitlement_pulls
    ADD CONSTRAINT entitlement_pulls_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

ALTER TABLE library_entries
    DROP CONSTRAINT library_entries_identity_sub_fkey;
ALTER TABLE library_entries
    ADD CONSTRAINT library_entries_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

ALTER TABLE library_exclusions
    DROP CONSTRAINT library_exclusions_identity_sub_fkey;
ALTER TABLE library_exclusions
    ADD CONSTRAINT library_exclusions_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

ALTER TABLE user_consoles
    DROP CONSTRAINT user_consoles_identity_sub_fkey;
ALTER TABLE user_consoles
    ADD CONSTRAINT user_consoles_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

ALTER TABLE measured_sizes
    DROP CONSTRAINT measured_sizes_identity_sub_fkey;
ALTER TABLE measured_sizes
    ADD CONSTRAINT measured_sizes_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

ALTER TABLE collection_definitions
    DROP CONSTRAINT collection_definitions_identity_sub_fkey;
ALTER TABLE collection_definitions
    ADD CONSTRAINT collection_definitions_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

ALTER TABLE collection_runs
    DROP CONSTRAINT collection_runs_identity_sub_fkey;
ALTER TABLE collection_runs
    ADD CONSTRAINT collection_runs_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

-- job_runs.identity_sub is nullable (NULL for a global, admin-scoped 'enrichment' run). Cascade still
-- applies to the rows that do carry a sub; the NULL rows are unaffected by a user delete.
ALTER TABLE job_runs
    DROP CONSTRAINT job_runs_identity_sub_fkey;
ALTER TABLE job_runs
    ADD CONSTRAINT job_runs_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

-- Child chains hanging off those per-user parents.

ALTER TABLE entitlement_snapshots
    DROP CONSTRAINT entitlement_snapshots_pull_id_fkey;
ALTER TABLE entitlement_snapshots
    ADD CONSTRAINT entitlement_snapshots_pull_id_fkey
        FOREIGN KEY (pull_id) REFERENCES entitlement_pulls (pull_id) ON DELETE CASCADE;

ALTER TABLE collection_items
    DROP CONSTRAINT collection_items_run_id_fkey;
ALTER TABLE collection_items
    ADD CONSTRAINT collection_items_run_id_fkey
        FOREIGN KEY (run_id) REFERENCES collection_runs (run_id) ON DELETE CASCADE;

ALTER TABLE console_installs
    DROP CONSTRAINT console_installs_console_id_fkey;
ALTER TABLE console_installs
    ADD CONSTRAINT console_installs_console_id_fkey
        FOREIGN KEY (console_id) REFERENCES user_consoles (console_id) ON DELETE CASCADE;

ALTER TABLE collection_runs
    DROP CONSTRAINT collection_runs_definition_id_fkey;
ALTER TABLE collection_runs
    ADD CONSTRAINT collection_runs_definition_id_fkey
        FOREIGN KEY (definition_id) REFERENCES collection_definitions (definition_id) ON DELETE CASCADE;
