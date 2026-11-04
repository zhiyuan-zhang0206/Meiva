-- The audit machine dimension has three states, and the table could express only one (task #6089).
--
-- A process whose identity reader raised `MachineNameMissing` wrote `socket.gethostname()`
-- instead, so `audit_events.machine` carried `Zyons-Mac-mini.local` beside `macmini` for the
-- same host: 741 rows over two weeks, and every reader that grouped by `machine` counted one
-- machine as two. The substitution was silent, so nothing pointed at it.
--
-- From here the values are `{canonical machine name} | unknown`, and a hostname is never an
-- identity: it moves to `machine_hostname`, which answers "which box wrote this row" without
-- standing in for the machine. `unknown` means the identity could not be read and is not
-- guessed; `base/telemetry/machine_identity.py` owns the mapping from the hostnames already in
-- the table.
--
-- No backfill: `audit_events` is evidence of what was written at the time. The four historical
-- hostnames stay in `machine` and are mapped on read by `audit_events_canonical`, so the ledger
-- keeps saying what it said while readers see one name per machine.
--
-- `machine_hostname` is nullable because every existing row predates the column.

ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS machine_hostname TEXT;

-- meiva:note
-- The single read path for the machine dimension: readers select this view instead of the
-- column, so exactly one place knows the four historical hostnames. Rows keep their `machine`
-- value; `machine_canonical` adds the mapped name and `machine_mapped` says whether it was
-- recognized. A value the mapping does not know (a retired machine, `unknown`) is passed
-- through unchanged.
CREATE OR REPLACE VIEW audit_events_canonical AS
SELECT
    a.*,
    CASE a.machine
        WHEN 'Zyons-Mac-mini.local' THEN 'macmini'
        WHEN 'zhangzhiyuandeMac-mini.local' THEN 'company-mini'
        WHEN 'Zhiyuans-MacBook-Air-606.local' THEN 'macbook-air'
        WHEN 'zhiyuanzhangs-MacBook-Air.local' THEN 'company-air'
        ELSE a.machine
    END AS machine_canonical,
    COALESCE(a.machine IN (
        'Zyons-Mac-mini.local',
        'zhangzhiyuandeMac-mini.local',
        'Zhiyuans-MacBook-Air-606.local',
        'zhiyuanzhangs-MacBook-Air.local'
    ), false) AS machine_mapped
FROM audit_events a;
