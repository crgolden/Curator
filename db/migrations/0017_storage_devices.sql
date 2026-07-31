-- Curator schema — migration 0017 (storage devices)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- WP3: consoles and storage are separate entities. A console's own built-in capacity stays exactly
-- where it already was (user_consoles.raw_capacity_gb/update_buffer_gb, unchanged) -- that storage never
-- detaches, so console_installs (keyed console_id, game_id) is untouched by this migration and keeps
-- working exactly as it does today for games installed on a console's own drive.
--
-- storage_devices covers only SWAPPABLE storage (M.2 expansion drives, USB drives) -- capacity_gb/
-- buffer_gb per device, and console_id nullable (NULL = currently unattached to any console).
--
-- storage_device_installs deliberately has NO console_id column, even though every install is
-- functionally "on whichever console this device is plugged into right now". Storing console_id
-- redundantly on each install row would go stale the moment a device is physically moved -- the /faq
-- page already documents the required behavior: "Move an M.2 or a USB drive to another PlayStation and
-- your games come with it... Librarian follows the drive rather than the console." Deriving "which
-- console" via storage_devices.console_id at read time makes that true for free: re-attaching a device
-- is one UPDATE on storage_devices, not a bulk rewrite of every install row it carries. This is also why
-- moving a device is not a violation of the non-auto-transfer invariant console_installs enforces --
-- that invariant is about checked state jumping to a DIFFERENT physical drive on console reassignment,
-- not about a drive's own installs following the drive it physically lives on.
--
-- 'internal' is deliberately not a storage_devices kind -- a console's built-in drive is not swappable
-- and is not represented as a row here at all; see user_consoles above. Playability: 'm2' behaves like
-- internal storage (both PS4 and PS5 titles run directly from it); 'usb' can hold PS4 titles but never
-- PS5 titles (physically cannot run from external USB storage) -- enforced at
-- PUT /consoles/{console_id}/installs/{game_id} and the new device-scoped install route, not in the
-- schema (CHECK constraints here would need a join to games.platform, which Postgres CHECK cannot do).

CREATE TABLE storage_devices
(
    device_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_sub UUID NOT NULL REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    console_id   UUID NULL REFERENCES user_consoles (console_id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('m2', 'usb')),
    capacity_gb  NUMERIC(8, 2) NOT NULL,
    buffer_gb    NUMERIC(8, 2) NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (identity_sub, name)
);

CREATE INDEX idx_storage_devices_identity_sub ON storage_devices (identity_sub);
CREATE INDEX idx_storage_devices_console_id ON storage_devices (console_id);

CREATE TABLE storage_device_installs
(
    device_id  UUID NOT NULL REFERENCES storage_devices (device_id) ON DELETE CASCADE,
    game_id    UUID NOT NULL REFERENCES games (game_id),
    installed  BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, game_id)
);

CREATE INDEX idx_storage_device_installs_game_id ON storage_device_installs (game_id);
