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

ALTER TABLE collection_definitions
    ADD COLUMN include_inactive BOOLEAN NOT NULL DEFAULT false;
