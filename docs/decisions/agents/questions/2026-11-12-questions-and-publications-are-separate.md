# Questions and publications are separate things, named for what they are

## Context

Two things reach the user from an agent: what the agent publishes (`meiva.ui.publish`, the unread
position) and what it asks (`meiva.ui.ask`, the question bar). Their storage had already diverged
(`agent_user_messages` and `agent_user_seen` for publications, `agent_notices` for questions), but the
questions kept the name "notice" after every non-question notice was removed, and "notice" is also
the word for system notices (completion, closure, permission, connection). One word for two
concepts made routes, CLI verbs, event kinds, components and docs ambiguous.

## Decision

A question is a question and a notice is a notice. Publications and questions share nothing but the
thin keyed-operation receipts.

- Tables: `agent_questions` (was `agent_notices`), `agent_publications` and `agent_publication_seen`
  (were `agent_user_messages`, `agent_user_seen`), `keyed_operation_receipts` (was
  `notice_operation_receipts`, used by questions, the seen cursor and reply-suggestion feedback).
  One migration renames the tables with their sequences, keys, constraints and indexes, so ids
  (visible to agents) and grants are kept. It also deletes the historical rows that were not
  questions, makes `question` NOT NULL and narrows `resolution` to answered, dismissed and withdrawn.
- Code: `gateway/agents/questions/` owns asks, close, the open feed and resolution;
  `gateway/agents/keyed_operations.py` owns the operation key and receipts; the contract module is
  `base/api_contracts/question.py`.
- Names that leave the repository's vocabulary: `/api/notices*` -> `/api/questions*`,
  `meiva notices` -> `meiva questions`, live events `notice_posted` / `notice_resolved` ->
  `question_asked` / `question_resolved`, inbound sources `system:notice-reply` / `notice-dismiss`
  -> `system:question-reply` / `question-dismiss`, the run-timeline link kind `notice` -> `question`,
  `display.notices_open_default_limit` -> `questions_open_default_limit`.
- Not renamed, because they are system notices and not questions: completion and closure notices,
  `ops/pty_close_notices`, permission, harness, connection and open-tasks notices, and the
  `system:`-family source test `is_system_notice_source`.

## Rejected

- Keeping `agent_notices` as the table name and renaming only the code: the table name is what
  operators read in SQL and what every reader of the schema sees first.
- Expand-contract with a copy and dual write: the tables are small, have no hot writer, and a rename
  keeps rows, ids and grants; the old name is simply unread after the cluster is stopped and
  migrated.
