-- Drop agent_harness_messages, the legacy stored timeline table for external harnesses
-- (docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md). Every external harness
-- (Claude Code, Codex, Antigravity) now projects its timeline directly from the CLI's own record
-- file on disk when asked for, and no code reads or writes this table. The grants, sequence, and
-- indexes go with the table.
DROP TABLE IF EXISTS agent_harness_messages;
