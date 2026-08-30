-- Curator schema — migration 0052 (collection_definitions install target console)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Adds collection_definitions.install_target_console_id: the ONE console a definition is "for". It exists
-- so a collection can show which of its games are already installed on that console, leaving the user the
-- remaining set to act on, and so marking a game installed from inside the collection has an unambiguous
-- destination instead of asking which device was meant.
--
-- This is the SECOND console-shaped column on this table and the two mean opposite things. Read the names
-- rather than assuming:
--
--   exclude_installed_on        UUID[]  consoles whose currently-installed games are REMOVED from the
--                                       candidate pool entirely ("what is left for my Vita that is not
--                                       already on my PS5"). A filter over what the collection contains.
--   install_target_console_id   UUID    the console the collection is aimed AT. Changes nothing about
--                                       membership; it decides whose install state is displayed and where
--                                       an install toggle writes.
--
-- A definition may legitimately carry both, and pointing them at the same console is self-defeating rather
-- than invalid -- every game installed there is excluded from the pool, so the install column would render
-- empty by construction. That is a UI warning, not a constraint: the database cannot tell an intentional
-- "show me only what is missing" from a mistake, and a CHECK forbidding it would also forbid the former.
--
-- Nullable, because a collection need not target a console; most do not, and the display simply omits the
-- column. A real foreign key is used here where exclude_installed_on could not have one (Postgres has no
-- array-element FK), with ON DELETE SET NULL so deleting a console un-targets its collections rather than
-- deleting them or leaving a dangling id. Retargeting is then an ordinary UPDATE.
--
-- Install state stays strictly per device: the display join reads console_installs for this console only
-- and does NOT union the storage_device_installs of drives attached to it. Carrying a checkmark between
-- devices was built once, found wrong, and reverted; a game on a USB drive is not installed on the
-- console's own storage, and the whole point of the column is to say what still has to be moved.
--
-- No backfill. Nothing can be inferred about which console an existing definition was meant for, and
-- guessing from exclude_installed_on would invert the meaning.

ALTER TABLE collection_definitions
    ADD COLUMN install_target_console_id UUID NULL REFERENCES user_consoles (console_id) ON DELETE SET NULL;

CREATE INDEX idx_collection_definitions_install_target_console
    ON collection_definitions (install_target_console_id)
    WHERE install_target_console_id IS NOT NULL;
