ALTER TABLE psn_catalog_cache
    ADD COLUMN concept_fetched_at TIMESTAMPTZ;

COMMENT ON COLUMN psn_catalog_cache.concept_fetched_at IS
    'When PSN''s concepts endpoint was last resolved for this title. NULL marks a row seeded by a '
    'storefront sweep for its cover art alone, whose catalog fields are absent rather than known-empty.';
