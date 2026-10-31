---
type: doc
title: Tool Execution — Fault-Isolated Code Execution
description: 'The agent''s sole tool—`execute_code(code: str)`—executes Python code in a disposable child process. This is the agent''s only way to interact with the outside world.'
tags: []
---

# Tool Execution — Fault-Isolated Code Execution

## What it is

The agent's sole tool—`execute_code(code: str)`—executes Python code in a disposable child process. This is the agent's only way to interact with the outside world. The process boundary provides cancellation and fault isolation; it is not a security sandbox.

## Core Mechanism

### Execution Flow (`node.py`)
- The LLM outputs one or more `execute_code` tool calls; each is executed sequentially in a fresh child with its own ToolMessage and ordinary graph state commit. Pending calls route `exec -> exec` after that commit. Calls come from LangChain's native `AIMessage.tool_calls`, normalized by `tool_calls.py`; each child receives its own `args["code"]`
- **Child lifecycle**: `run_in_subprocess` (`_subprocess.py`) refuses the call before any request file exists when the interpreter's editable-install records are invalid, then spawns one disposable child per exec (`python -I -X utf8 -m agent.execution.child`, same venv; the child leads its own session and process group) and polls it every 50ms. A stuck native call is hard-killable without touching the agent process (issue #184), and persistent `meiva.shell.sessions` live outside the child, so they survive domain close. Two hard timeouts apply (per-code `exec_timeout_seconds` cluster default 300s, overridable per agent via `config_overlay`; graph-level `exec_node_timeout_seconds` default 1200s), and every exit path ends in the same group kill, reap, bounded pipe drain and file cleanup: [[agent/graph/exec/docs/child-lifecycle.meiva.okf.md|Exec Child Lifecycle]]
- **Output streaming and accumulation**: the child's merged stdout/stderr is drained into a `StreamingTextIO` and streamed to Redis in real time, a silent live child gets SSE-only keepalives, and `exec_output_accumulation_max_chars` (default 1,000,000) bounds a runaway `print` loop by truncating with a marker rather than killing the exec. The parent finally returns the unchanged accumulator envelope to the LLM ([[agent/graph/exec/docs/output-streaming.meiva.okf.md|Output Streaming]])
- `exec_child_boot` measures child setup after imports, immediately before user code. Parent timing covers the complete exec lifecycle.
- `meiva.self.attach()` rides the plugin-state result envelope. Ordinary exec merges attachments, appends their media HumanMessage after the result and any context notes, and clears drained attach state. The model sees the media on its next step in the same turn. Claim retains a fallback drain for paths that skip this update, including compact halt.

### Execution Environment
- Each execution uses a fresh `fresh_globals` (`__name__="__agent_code__"`)
- `meiva.*` SDK is available, but **not auto-imported**—agent code must explicitly `import meiva`
- `meiva.cwd` is logical plugin state, not a process-global `chdir`: MeivaCode's `files` / `shell` / `understand` wrappers resolve it explicitly, while bare `open`, `Path.cwd`, imports, and user subprocesses retain the disposable child's stable OS cwd

### Output Handling
- `agent/graph/exec/output.py:wrap_code_output()` wraps the result (cancelled/timed_out markers) from an `OutputLimits` value (archive directory, `CropLimits`, timeout, inline cap) and the caller's `now`; it reads no configuration, clock or workspace path
- Configurable soft line previews and the two existing hard caps have distinct retention contracts; see [[agent/graph/exec/docs/output-preview.meiva.okf.md]]. Soft archives protect current context references within a byte budget; legacy hard-overflow files retain their 20-file ring
- Results are dispatched by sum type (`ExecDone|ExecCancelled|ExecTimedOut|ExecLifecycle|ExecCrashed`, strict `_result.py`; a lifecycle outcome is a closed `ExecLifecycleKind`, so the parent never imports the exception classes). `_dispatch.plan_dispatch` decides, as values, the outcome kind, whether the turn halts, the status, the envelope body (a compaction's note, an empty crash's honest verdict) and the markers; `node.dispatch_exec_result` wraps, logs and announces that plan. Runtime imports occur under the child entry guard, so boot/config and user-code exceptions become a `crashed` envelope with their traceback; lifecycle exceptions (terminate/restart/compact) take highest priority
- Each exec-result ToolMessage carries `additional_kwargs["sdk_calls"]` — the run's real top-level `meiva.*` call tally, counted by `base.agents.sdk.telemetry` (`[]` = ran, called none; omitted = unknown, e.g. a boot crash). The timeline's collapsed-code chip renders these counts only — no source-text scan.

### In-memory system-note injection (`_notes.py`, user ruling 2026-08-11)
- AGENTS.md / CLAUDE.md context notes (meiva_code plugin) ride the exec's own messages delta, placed after all of the assistant message's tool results; the exec child's prompt-injection findings take a separate route through `state.security_findings` and the after_exec hook. Placement and the wire-contract reason: [[agent/graph/exec/docs/exec-notes.meiva.okf.md|Exec Note Injection]]

## Key Dependencies

- [[sdk-surface.meiva.okf.md]] — The agent calls all `meiva.*` tools through `execute_code`
- [[agent/graph/docs/graph.meiva.okf.md]] — `exec_node_impl` is the second node of the execution graph
- [[system-prompt.meiva.okf.md]] — The system prompt specifies output format (text + optional execute_code)

## Entry Points

- `agent/graph/exec/node.py:exec_node()` — [[process.meiva.okf.md|child process boundary]] and sum dispatch
- `agent/graph/exec/_stream.py` — `StreamingTextIO` + `ExecOutputChunkPublisher` (streaming output incremental push)
- `agent/graph/exec/_dispatch.py`, `_poll.py`, `_guard.py`, `_result.py` — the strict decision modules: result dispatch plan, poll-beat plan, editable-install refusal text and the result sum type
- `agent/graph/tool_calls.py` — Per-call normalization preserving every invocation and result
- `agent/graph/exec/output.py` — Output envelope formatting
- `agent/graph/exec/_crop.py` — Soft line previews and bounded, reference-protected archives

## Notes

- Hosted turn cancel and exec cleanup: [[hosted-turn-cancel.meiva.okf.md]].
- Process bootstrap ownership: [[bootstrap.meiva.okf.md]].
- Long-running operations should not go through `execute_code`: one-time long commands use `meiva.shell.run_background` (auto-reports on completion), interactive/long-lived processes use `meiva.shell.sessions` persistent sessions

Related test premise: [Subprocess test assumptions](subprocess-tests.meiva.okf.md).
