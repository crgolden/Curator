-- Links a user's modelled console (user_consoles) to one of their PSN-registered devices, as seen through
-- GET /devices. The two keys are the 1:1 rule, enforced by the schema rather than by service code: the
-- primary key stops one console linking to several devices, the unique constraint stops one device linking
-- to several consoles.
--
-- device_id is deliberately NOT a foreign key. Registered devices live in PSN, not here -- GET /devices is
-- a live proxy with nothing persisted behind it -- so a device disappearing from PSN must leave the user's
-- own mapping decision intact rather than cascading it away.
CREATE TABLE console_device_links
(
    identity_sub UUID NOT NULL REFERENCES app_users (identity_sub) ON DELETE CASCADE,
    console_id   UUID NOT NULL REFERENCES user_consoles (console_id) ON DELETE CASCADE,
    device_id    TEXT NOT NULL,
    linked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (identity_sub, console_id),
    UNIQUE (identity_sub, device_id)
);

CREATE INDEX idx_console_device_links_identity_sub ON console_device_links (identity_sub);
