---
type: doc
title: History Dump — Pre-Compact JSONL Dump of the Conversation
description: The pre-compact JSONL dump of the full conversation written by both compaction paths before the history is wiped, and how it takes the workspace directory and the write moment as values so tests hand in a temp path and a fixed instant.
tags: []
---

# History Dump — Pre-Compact JSONL Dump of the Conversation

## Responsibility

`agent/hooks/history_dump.py:dump_history()` — pre-compact JSONL dump of the full conversation, written by both compaction paths (`agent/graph/claim/_decide.py` and `agent/graph/hooks/compact.py`) before the history is wiped. It takes the agent's workspace directory and the write moment as values (`dump_history(messages, workspace, config, *, now)`); the file name is `agent/hooks/dump_name.py:dump_filename`, a `strict` function of the messages and `now`, so a test hands in `tmp_path` and a fixed instant instead of replacing `workspace_dir` or the clock.

The hook layer around it is [[agent/hooks/docs/hooks.meiva.okf.md|Agent Hooks]]'s to state.

## Entry Points

- `agent/hooks/history_dump.py` — `dump_history`, the dump writer
- `agent/hooks/dump_name.py` — `dump_filename`, the pure file name

Parent: [[agent/hooks/docs/hooks.meiva.okf.md|Agent Hooks]].
