-- Curator schema — migration 0039 (entitlement_snapshots: one row per entitlement, not per pull)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- entitlement_snapshots was keyed (pull_id, entitlement_id), so every refresh rewrote the user's whole
-- library as a new generation of rows and the table grew without bound. The key becomes
-- (identity_sub, entitlement_id): one durable row per entitlement per user, carrying first_seen_at and
-- last_seen_at instead of a row per observation. pull_id survives as "the most recent pull that saw
-- this row" and becomes nullable, ON DELETE SET NULL, so the row outlives any single pull.
--
-- The backfill runs in three steps because each depends on the previous: fill identity_sub from the
-- owning pull; derive first/last seen from the min/max pulled_at across each entitlement's history;
-- then delete all but the most recent row per (identity_sub, entitlement_id) before the new primary
-- key can be added. Ordering is by pulled_at DESC with pull_id as a tiebreak so the surviving row is
-- deterministic when two pulls share a timestamp.
--
-- The index this creates duplicates 0035's, and 0041 consolidates both into one ordered index. That is
-- deliberately left to 0041 rather than corrected here: these two migrations ship together, so the
-- duplicate never exists on a deployed database for longer than one migration run, and rewriting this
-- file to drop an index 0041 already drops would leave 0041's DROP dangling. Read 0041's header for why
-- (title_id, last_seen_at DESC) is the shape the cover-art query actually needs.

ALTER TABLE entitlement_snapshots
    ADD COLUMN identity_sub UUID,
    ADD COLUMN first_seen_at TIMESTAMPTZ,
    ADD COLUMN last_seen_at TIMESTAMPTZ;

UPDATE entitlement_snapshots es
SET identity_sub = ep.identity_sub
FROM entitlement_pulls ep
WHERE ep.pull_id = es.pull_id;

WITH ranked AS (
    SELECT es.ctid,
           row_number() OVER (
               PARTITION BY es.identity_sub, es.entitlement_id
               ORDER BY ep.pulled_at DESC, es.pull_id
           ) AS recency,
           min(ep.pulled_at) OVER (PARTITION BY es.identity_sub, es.entitlement_id) AS first_pulled,
           max(ep.pulled_at) OVER (PARTITION BY es.identity_sub, es.entitlement_id) AS last_pulled
    FROM entitlement_snapshots es
    JOIN entitlement_pulls ep ON ep.pull_id = es.pull_id
)
UPDATE entitlement_snapshots es
SET first_seen_at = ranked.first_pulled,
    last_seen_at = ranked.last_pulled
FROM ranked
WHERE ranked.ctid = es.ctid;

DELETE FROM entitlement_snapshots es
USING (
    SELECT es2.ctid,
           row_number() OVER (
               PARTITION BY es2.identity_sub, es2.entitlement_id
               ORDER BY ep.pulled_at DESC, es2.pull_id
           ) AS recency
    FROM entitlement_snapshots es2
    JOIN entitlement_pulls ep ON ep.pull_id = es2.pull_id
) stale
WHERE stale.ctid = es.ctid AND stale.recency > 1;

ALTER TABLE entitlement_snapshots
    DROP CONSTRAINT entitlement_snapshots_pkey;

ALTER TABLE entitlement_snapshots
    ALTER COLUMN identity_sub SET NOT NULL,
    ALTER COLUMN first_seen_at SET NOT NULL,
    ALTER COLUMN last_seen_at SET NOT NULL,
    ALTER COLUMN pull_id DROP NOT NULL,
    ADD PRIMARY KEY (identity_sub, entitlement_id);

ALTER TABLE entitlement_snapshots
    DROP CONSTRAINT entitlement_snapshots_pull_id_fkey,
    ADD CONSTRAINT entitlement_snapshots_pull_id_fkey
        FOREIGN KEY (pull_id) REFERENCES entitlement_pulls (pull_id) ON DELETE SET NULL,
    ADD CONSTRAINT entitlement_snapshots_identity_sub_fkey
        FOREIGN KEY (identity_sub) REFERENCES app_users (identity_sub) ON DELETE CASCADE;

CREATE INDEX idx_entitlement_snapshots_title_id_with_art
    ON entitlement_snapshots (title_id)
    WHERE COALESCE(title_image_url, concept_icon_url, game_icon_url) IS NOT NULL;

COMMENT ON TABLE entitlement_snapshots IS
    'One row per (identity_sub, entitlement_id), not per pull.';

COMMENT ON COLUMN entitlement_snapshots.pull_id IS
    'The most recent pull that observed this entitlement. Nullable: the row outlives any single pull.';

COMMENT ON COLUMN entitlement_snapshots.first_seen_at IS
    'When this entitlement was first observed for this user. Never updated after insert.';
