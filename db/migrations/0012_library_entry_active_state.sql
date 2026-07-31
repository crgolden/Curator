-- Curator schema — migration 0012 (per-user entitlement active state)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- PSN reports an activeFlag per entitlement: false means the user's access has ended (a PS Plus title
-- that left the catalog, a lapsed subscription), true or absent means they can still play it. It has
-- always been captured in entitlement_snapshots.active -- but canonicalization threw those entitlements
-- away before they could reach library_entries, so nothing downstream could ever see them.
--
-- That had two consequences:
--
--   1. A user could not choose. "Only what I can play right now" was the hardcoded and only behavior;
--      "everything I ever had access to" was unreachable, even though the data was sitting in
--      entitlement_snapshots.
--   2. Worse, the library silently over-reported. library_entries is written by an upsert with no delete
--      pass, so when a title lapsed it disappeared from the canonicalized set, nothing removed or
--      flagged the existing row, and the game stayed in the user's library forever -- listed as owned,
--      with no way to tell it was no longer playable. The drop intended to hide lapsed titles and in
--      practice froze them in place.
--
-- is_active fixes both: canonicalization now carries the flag through instead of filtering on it, so a
-- lapsed title flips to false on the next build rather than being stranded.
--
-- CRITICAL LAYERING CONSTRAINT: this column lives on library_entries and must never move to games or
-- game_enrichment. Active-ness is a fact about one user's access, not about the game. The same title is
-- routinely active for one user and inactive for another; a shared-catalog column would make one user's
-- lapsed subscription silently reclassify the game for everyone else.
ALTER TABLE library_entries
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;

CREATE INDEX idx_library_entries_is_active ON library_entries (identity_sub, is_active);

-- The matching authoring choice on a collection: by default a collection is built from what the owner
-- can actually play, and include_inactive opts into the full historical library instead. Stored
-- alongside the other filter columns as provenance -- it describes how the collection was assembled,
-- and is re-applied when the owner asks for a fresh proposal via POST /collections/{id}/runs.
ALTER TABLE collection_definitions
    ADD COLUMN include_inactive BOOLEAN NOT NULL DEFAULT false;

-- NOT addressed here, deliberately: an entitlement that vanishes from PSN's response entirely (rather
-- than being reported with activeFlag = false) still leaves a stale library_entries row. Fixing that
-- means deactivating every entry absent from the latest pull, and entitlements() stops paging on a short
-- page -- so a truncated PSN response is indistinguishable from a genuinely small library, and a sweep
-- would silently deactivate a user's whole library with no way back short of a re-pull. PS Plus lapses
-- produce case 1 (an explicit false), which is what this migration handles. The sweep needs its own
-- guard (compare against the previous entry count) and its own decision.
