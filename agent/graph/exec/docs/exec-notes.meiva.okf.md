---
type: doc
title: Exec Note Injection — Context Notes Riding the Exec Result
description: How context notes (AGENTS.md / CLAUDE.md from the meiva_code plugin) travel in the exec's own messages delta, why they are placed after all of an assistant message's tool results, and how the exec child's prompt-injection findings take a separate route.
tags: []
---

# Exec Note Injection — Context Notes Riding the Exec Result

## In-memory system-note injection (`_notes.py`, user ruling 2026-08-11)

- AGENTS.md / CLAUDE.md context notes (meiva_code plugin) ride the exec's own messages delta — no side-channel file. `agent/graph/exec/_notes.py:merge_exec_notes()` places them after all of the assistant message's tool results (intermediate steps park them in `pending_exec_notes`) because the Anthropic-compat wire contract requires `tool_use` to be immediately followed by `tool_result`; a compact (`SystemHalt`) drops them
- The exec child's prompt-injection findings do not pass through here: the exec node commits them to `state.security_findings` and the after_exec hook `agent/hooks/security.py` delivers them as SECURITY notes ([[agent/graph/docs/inbound-findings.meiva.okf.md]]); a compacting exec drops them with the history they annotate

## Entry Points

- `agent/graph/exec/_notes.py` — `merge_exec_notes()`, the placement of notes behind the tool results

Parent: [[agent/graph/exec/docs/exec.meiva.okf.md|Tool Execution]].
