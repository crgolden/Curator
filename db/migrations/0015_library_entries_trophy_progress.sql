-- Curator schema — migration 0015 (persisted trophy progress)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- 0001_initial.sql declined to give trophy data a Postgres table on the grounds that it is "time-decaying
-- current-state data, not something needing permanent history". That reasoning holds for a trophy *title
-- list* and for presence/social/devices, which are all live-proxy or short-TTL cache by design. It does not
-- hold for one number per game the user already owns:
--
--   * Membership is already durable. library_entries is the row this hangs off, and 0014 already persisted
--     the np_communication_id linking that row to its PSN trophy title. Progress is the last piece of the
--     same fact still being re-derived from scratch on every read.
--   * Keeping it out of Postgres made min_percent_completed impossible to express in SQL. Filtering
--     "games I am over 80% through" required fetching every game's trophy data first and filtering in
--     Python afterwards -- which is precisely why the collection routes pulled the caller's entire library
--     on every preview, regardless of how narrow the spec was.
--   * It put a live PSN dependency on ordinary page loads. A stale token, a PSN outage, or a cold Redis
--     silently blanked the column.
--
-- The freshness argument is answered by trophy_progress_fetched_at plus the existing library-refresh job,
-- which is exactly how every other third-party signal in this schema is handled (rawg_cache,
-- opencritic_cache, psn_catalog_cache, game_enrichment all carry a fetched-at and go stale between runs).
--
-- PRIVACY NOTE, since the no-Postgres rule has been described elsewhere as a privacy tenet: it was not one.
-- 0001's own wording is about data lifecycle, and a 15-minute Redis cache on the same host under the same
-- operator is not a privacy control -- if the data were too sensitive to store it would be too sensitive to
-- cache. The real obligations are the ones below, and they have teeth where a storage ban did not:
--
--   * These columns live on library_entries, whose identity_sub foreign key cascades (0009), so DELETE /me
--     removes them with the rest of the account.
--   * They are PSN-derived and therefore gated on psn_links.harvest_trophies. Turning that preference off
--     must CLEAR any already-stored progress, not merely stop refreshing it -- see
--     curator.library.repository.LibraryRepository.clear_trophy_progress.
--   * DELETE /psn/link must clear it too. Unlinking removes the psn_links row and with it the
--     harvest_trophies flag, so anything left here would outlive every control over it -- and GET /library
--     does no read-time PSN gating at all any more, so it would go on serving those percentages forever.
--     It would also be incoherent for the weaker action (flipping the toggle off) to erase while the
--     stronger one preserved. The rest of the library deliberately survives an unlink; this does not.

ALTER TABLE library_entries
    ADD COLUMN trophy_percent_completed  SMALLINT CHECK (trophy_percent_completed BETWEEN 0 AND 100),
    ADD COLUMN trophy_progress_fetched_at TIMESTAMPTZ;

-- Supports the min_percent_completed predicate now that it can be applied in the candidate query rather
-- than after the fact. Partial: rows with no progress can never satisfy a minimum, so they are dead weight
-- in the index.
CREATE INDEX idx_library_entries_trophy_progress
    ON library_entries (identity_sub, trophy_percent_completed)
    WHERE trophy_percent_completed IS NOT NULL;
