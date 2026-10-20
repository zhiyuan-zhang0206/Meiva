-- A planned window around an intentional stop: `meiva stop --maintenance-for 45m
-- --reason "..."` records when this host is expected back, so the "stopped too long"
-- rule can tell a planned maintenance from a forgotten stop instead of reporting
-- every window as a fault (task #5464). Host-level, not per unit: the window
-- describes when the host returns, and the unit row only carries the announcement.
ALTER TABLE machines
    ADD COLUMN IF NOT EXISTS maintenance_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS maintenance_reason TEXT;

COMMENT ON COLUMN machines.maintenance_until IS
    'When the operator says this host returns (meiva stop --maintenance-for); NULL on an open-ended stop';
COMMENT ON COLUMN machines.maintenance_reason IS
    'The operator''s one-line reason for the window, as typed';
