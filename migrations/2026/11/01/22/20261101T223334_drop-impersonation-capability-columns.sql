-- Re-issue of the delta the 2026-11-01 baseline reset deleted.
--
-- `20261029T101641_drop-impersonation-capability-columns` (#7683, 2026-10-29) never
-- applied anywhere: it landed while five deployments still ran a tree whose
-- `base/cluster/authority/event_grants.py` named these two columns in a startup
-- GRANT, so the drop waited for a wave that shipped the narrower grant list first.
-- #8136 (2026-11-01) then folded the preceding deltas into `db/schema.sql` and
-- deleted that file. `db/schema.sql` describes the table without these columns, so
-- a fresh database never creates them -- but the deployments that already hold
-- them had no migration left that would ever drop them.
--
-- Nothing reads or writes them. `5c179ce2e` (#6347, 2026-10-19) reverted the lease
-- capability declaration gate and kept the columns as inert records; every lease
-- created since is NULL in both, and the four leases that carry a value predate the
-- revert and have not changed. Their values stay in a protected backup outside the
-- repository and are deliberately not reproduced here or in the pull request.
--
-- Ordering: this is the contract step, and the code side has been on main since
-- #7683 -- the grant lists no longer name either column. `meiva start` applies
-- migrations before the pooler step, so the process that drops them is the process
-- running the new list. Do not hand-apply this file against a deployment that still
-- carries the old list: its next cold start would fail on
-- `column "capabilities" does not exist`.
--
-- A bounded wait: the table holds ~168 rows and the drop needs ACCESS EXCLUSIVE only
-- briefly. Unbounded, a lingering reader would make the ALTER queue every later read
-- and write of the lease table behind itself, and a normal start would surface that
-- as a stuck start rather than a failed migration. The applier runs each file inside
-- one explicit transaction, so `SET LOCAL` covers exactly these statements.
-- `IF EXISTS` makes a retry after a timeout safe and makes this file a no-op on a
-- fresh database.
SET LOCAL lock_timeout = '3s';

ALTER TABLE agent_impersonations
    DROP COLUMN IF EXISTS capabilities,
    DROP COLUMN IF EXISTS capability_declared_by;
