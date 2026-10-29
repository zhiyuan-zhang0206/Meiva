-- Drop the two columns a repealed design left behind (task #5335).
--
-- `5c179ce2e` (PR #6347, 2026-10-19) reverted the lease capability declaration gate and
-- kept `capabilities` / `capability_declared_by` as "inert records". Nothing has written or
-- read them since, `db/schema.sql` declared them while no migration did (so a database
-- built from the baseline and one built from the schema disagreed), and two unread columns
-- on the core lease table are a standing invitation to revive a design the user repealed:
-- "a takeover declares nothing and may use the whole SDK surface".
--
-- This is the contract step of expand-contract: the code that used the columns has been
-- gone since #6347.
--
-- Destructive and irreversible. Four pre-revert leases still carried a value; the pull
-- request that adds this migration records their ids, values and timestamps verbatim,
-- because after this statement that is the only place those values exist.
ALTER TABLE agent_impersonations
    DROP COLUMN IF EXISTS capabilities,
    DROP COLUMN IF EXISTS capability_declared_by;
