-- Drop the two columns a repealed design left behind (task #5335).
--
-- `5c179ce2e` (PR #6347, 2026-10-19) reverted the lease capability declaration gate and
-- kept `capabilities` / `capability_declared_by` as "inert records". Nothing has read or
-- written them since: every lease created after the revert has NULL in both, and the only
-- non-NULL values (four leases, all created before it) have not changed. Two unread columns
-- on the core lease table are a standing invitation to revive a design the user repealed:
-- "a takeover declares nothing and may use the whole SDK surface".
--
-- This is the contract step of expand-contract. The code side has been gone since #6347,
-- and the cluster's code-version minimum (`deployment_state.min_code_version`) is above the
-- revert commit's version, so no deployed process reads or writes these columns. The shape
-- lives in `db/schema.sql` -- the 2026-10-26 reset folded their original delta into it --
-- and that file drops them in the same change.
--
-- Destructive and irreversible. The four rows that still carry a value are preserved
-- (ids, values, timestamps) in a protected backup kept outside the repository; they are
-- deliberately not reproduced here or in the pull request.
ALTER TABLE agent_impersonations
    DROP COLUMN IF EXISTS capabilities,
    DROP COLUMN IF EXISTS capability_declared_by;
