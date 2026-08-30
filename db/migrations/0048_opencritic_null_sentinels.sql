-- Curator schema — migration 0048 (repair the OpenCritic rows two fixed code paths left behind)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Both defects are fixed in code in both runtimes; neither fix rewrites the rows already written.
--
-- 1. opencritic_cache.tier held '' where NULL belongs. OpenCriticGame.Tier was non-nullable, so both
--    conversions into it manufactured a value and every round trip turned a real NULL into ''. The column
--    is nullable and the type is string? now. Until these rows are corrected, tier = '' and tier IS NULL
--    both mean "no tier", so every query filtering on one has to handle both — which is exactly the kind
--    of two-spellings-for-one-fact that outlives whoever remembers it.
--
-- 2. OpenCritic's -1 means "not enough reviews", on percentRecommended exactly as on topCriticScore. Both
--    runtimes guarded only the score, so -1 was stored as a real percentage. NullIfNoData covers both
--    fields now. game_enrichment.oc_percent_recommended is included for the same sentinel: it measured
--    clean in production, which is a property of that dataset rather than of the writer, and the predicate
--    makes it a no-op wherever that holds.
--
-- Idempotent by predicate, not by bookkeeping: every statement is a no-op on a database that already has
-- no rows matching. A fresh database (CI's service container) matches nothing and all four report 0.
--
-- top_critic_score is checked here rather than assumed. §15 measured it clean, and a clean column costs
-- one guarded UPDATE to keep clean; assuming it stays clean is what let percent_recommended accumulate 76
-- rows unnoticed in the first place.

UPDATE opencritic_cache SET tier = NULL WHERE tier = '';

UPDATE opencritic_cache SET percent_recommended = NULL WHERE percent_recommended < 0;

UPDATE opencritic_cache SET top_critic_score = NULL WHERE top_critic_score < 0;

UPDATE game_enrichment SET oc_percent_recommended = NULL WHERE oc_percent_recommended < 0;
