-- library_entry_platforms has been the platform of record since 0032, which copied these two booleans
-- into it. Both writers now name only that table (Curator's manual add, the Functions ingestion upsert),
-- and the last reader, the collections candidate query, sizes from it. Deploy after the Functions worker
-- that stopped writing them.

ALTER TABLE library_entries DROP COLUMN native_ps5;

ALTER TABLE library_entries DROP COLUMN ps4_eligible;
