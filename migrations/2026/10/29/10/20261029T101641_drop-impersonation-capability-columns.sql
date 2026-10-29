-- A bounded wait: this table holds ~168 rows and the drop needs ACCESS EXCLUSIVE only briefly.
-- Unbounded, a lingering reader would make the ALTER queue every later read and write of the lease
-- table behind itself — and because a normal `meiva start` applies this file before it finishes
-- booting, that queue would surface as a stuck start, not a failed migration. The applier runs each
-- file inside one explicit transaction (`base/deploy/schema/migrations.py`), so `SET LOCAL` covers
-- exactly this file's statements; the drop is idempotent, so a retry after a timeout is safe.
SET LOCAL lock_timeout = '3s';

-- Drop the two columns a repealed design left behind (task #5335).
--
-- `5c179ce2e` (PR #6347, 2026-10-19) reverted the lease capability declaration gate and
-- kept `capabilities` / `capability_declared_by` as "inert records". Nothing has read or
-- written them since: every lease created after the revert has NULL in both, and the only
-- non-NULL values (four leases, all created before it) have not changed. Two unread columns
-- on the core lease table are a standing invitation to revive a design the user repealed:
-- "a takeover declares nothing and may use the whole SDK surface".
--
-- This is the contract step of expand-contract: the code side has been gone since #6347.
--
-- The one remaining consumer is not a reader or a writer but a GRANT: an older deployed tree
-- still names these two columns in `base/cluster/authority/event_grants.py`, and every gateway
-- cold start runs `ensure_groups` -> `grant_event_log_runner_access` with those names hardcoded.
-- In `meiva start` the migration step runs *before* the pooler step, so a start that applied this
-- drop and then ran the old grant list would fail on `column "capabilities" does not exist`.
-- Dropping is therefore safe only once the gateway that migrates is running code where the grant
-- lists no longer name them -- which is what this change ships. Do not hand-apply this file
-- against a deployment that still carries the old list.
--
-- The shape lives in `db/schema.sql` -- the 2026-10-26 reset folded their original delta into
-- it -- and that file drops them in the same change.
--
-- Destructive and irreversible. The four rows that still carry a value are preserved
-- (ids, values, timestamps) in a protected backup kept outside the repository; they are
-- deliberately not reproduced here or in the pull request.
ALTER TABLE agent_impersonations
    DROP COLUMN IF EXISTS capabilities,
    DROP COLUMN IF EXISTS capability_declared_by;
