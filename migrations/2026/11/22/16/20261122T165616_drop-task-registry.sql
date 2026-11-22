-- Keep queued notes readable after the task note tag is removed from the runtime.
UPDATE inbound_messages
SET payload = jsonb_set(payload, '{note_tag}', '"context"')
WHERE kind = 'system_note' AND status IN ('pending', 'claimed')
  AND payload->>'note_tag' = 'task';

DROP TABLE IF EXISTS task_update_receipts RESTRICT;
DROP TABLE IF EXISTS task_creation_receipts RESTRICT;
DROP TABLE IF EXISTS task_assignment_receipts RESTRICT;
DROP TABLE IF EXISTS task_patch_receipts RESTRICT;
DROP TABLE IF EXISTS agent_tasks RESTRICT;
