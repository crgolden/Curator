-- Provenance for a library row. Every row until now came from a PSN entitlement pull; 'manual' rows are
-- games a user owns that PSN's entitlement API has no record of at all (physical discs, and anything else
-- never bought digitally on the linked account).
--
-- A discriminator on the existing table rather than a parallel manual-games table: every read path
-- (candidate pool, library page, profile library, scoring) would otherwise need forking, and the two
-- copies would drift.
ALTER TABLE library_entries
    ADD COLUMN source TEXT NOT NULL DEFAULT 'psn'
        CHECK (source IN ('psn', 'manual'));

ALTER TABLE library_entries
    ADD CONSTRAINT library_entries_psn_rows_have_entitlement
        CHECK (source <> 'psn' OR winning_entitlement_id IS NOT NULL);

CREATE INDEX idx_library_entries_source ON library_entries (identity_sub, source);
