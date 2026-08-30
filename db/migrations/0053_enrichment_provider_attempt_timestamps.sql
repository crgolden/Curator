-- Curator schema — migration 0053 (per-provider attempt timestamps)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- `game_enrichment` recorded when RAWG was last consulted (`rawg_attempted_at`, migration 0043) but had no
-- equivalent for PS Store or OpenCritic, so "we asked and it had nothing" and "we never asked" were
-- indistinguishable for two of the three providers. These two columns close that gap.
--
-- They are a RECORD, not a retry gate. The standing rule is that a provider is retried on every
-- opportunity -- a library refresh or a catalog-wide enrichment run alike -- until its own success flag
-- flips: `rawg_enriched`, `opencritic_enriched`, `psn_enriched`. The timestamps answer "when did we last
-- try", which is an operational question, and deliberately do not answer "should we try again", which
-- belongs to the flag. Gating a retry on a timestamp is what made a single failed attempt permanent.
--
-- PS Store is the source of truth for genre and star rating, and its data can only be fetched while the
-- user's own PSN session is live, so a refresh is the one opportunity to collect it. Skipping a title
-- because an unrelated provider had already answered for it is how 874 of 886 owned games ended up
-- permanently unreachable, carrying no genre at all.
--
-- No backfill. A NULL here means "never recorded an attempt", which is true of every existing row: the
-- column did not exist, so nothing can honestly be asserted about when those games were last tried.

ALTER TABLE game_enrichment
    ADD COLUMN psn_attempted_at TIMESTAMPTZ NULL,
    ADD COLUMN opencritic_attempted_at TIMESTAMPTZ NULL;
