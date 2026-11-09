# Exec Note Injection — Context Notes Riding the Exec Result

## In-memory system-note injection (`_notes.py`, user ruling 2026-08-11)

- AGENTS.md / CLAUDE.md context notes (meiva_code plugin) ride the exec's own messages delta — no side-channel file. `agent/graph/exec/_notes.py:merge_exec_notes()` places them after all of the assistant message's tool results (intermediate steps park them in `pending_exec_notes`) because the Anthropic-compat wire contract requires `tool_use` to be immediately followed by `tool_result`; a compact (`SystemHalt`) drops them
- The exec child's prompt-injection findings do not pass through here: the exec node commits them to `state.security_findings` and the after_exec hook `agent/hooks/security.py` delivers them as SECURITY notes ([Prompt-Injection Findings — Who Delivers Which SECURITY Note](../../docs/inbound-findings.md)); a compacting exec drops them with the history they annotate

## Entry Points

- `agent/graph/exec/_notes.py` — `merge_exec_notes()`, the placement of notes behind the tool results

Parent: [Tool Execution](exec.md).
