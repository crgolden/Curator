-- Curator schema — migration 0018 (console model)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- WP3: "auto-assign a default size from the chosen console model if none is given; flag loudly, never
-- refuse the edit". model is purely informational and optional -- it exists to drive
-- consoles_routes.py's default-capacity lookup at creation time, nothing else reads it, and
-- raw_capacity_gb always stays independently editable regardless of what model (if any) was recorded.

ALTER TABLE user_consoles
    ADD COLUMN model TEXT NULL;
