-- The product keeps one kind of open item: a question (`meiva.ui.ask`: a question payload, no
-- expiry). Notices that were not questions, and notices that carried an expiry the reaper
-- enforced, no longer have any writer, reader or reaper. Resolve what is still open of them as
-- 'expired' (legal for both require_response values) so no unresolved legacy row lingers in
-- the open-question reads. Columns and tables stay until a later contract migration.
UPDATE agent_notices
SET resolved_at = now(), resolution = 'expired'
WHERE resolved_at IS NULL
  AND (question IS NULL OR expire_at IS NOT NULL);
