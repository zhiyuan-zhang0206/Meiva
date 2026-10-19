---
type: doc
title: Antigravity (agy) session bridge
description: '`base/lm/harness/antigravity/` — one `agy --input-format stream-json` process as a turn engine: NDJSON transport, ordered inbound queue, event mapping, persisted agent-id ↔ conversation-id, interrupt that kills and respawns, and `denied_actions` treated as failure.'
tags:
- base
- library
- llm-inference
- antigravity
---

# Antigravity (agy) session bridge

`base/lm/harness/antigravity/` turns one Google Antigravity CLI (`agy`) process
into the runtime's turn engine. It reuses the shared contract modules
(`events`, `handoff`, `registry`, `process`) unchanged: what is special about
`agy` lives in this package.

## The transport is not ACP

`agy` 1.4.2 has no Agent Client Protocol server. The ACP surface is a separate
Google binary (`agy_acp_server.par`, release 1.3.0, ~926 MB unpacked) with its
own Google sign-in; it is not installed by `agy` and not used here. The CLI's own
programmatic surface is headless print mode:

```
agy --input-format stream-json --output-format stream-json --model <id> [--conversation <id>]
```

One process serves the whole session. stdin takes one JSON object per line,
`{"event":"user","message":{"content":"…"}}`; stdout emits `init`, `step_update`
and `result`. There are no request ids and no methods, so **a cancel cannot be a
request**: the adapter sends `SIGINT`, and the CLI answers with a final `result`
(`status: ERROR`, `error: interrupted`) and exits 1. `interrupt()` therefore
retires the process; the next turn respawns it with `--conversation`, which keeps
the transcript while the process is replaced.

If Antigravity ever ships ACP into the CLI, only `transport.py` changes.

## Three harness differences, absorbed here

| Difference | Where it is absorbed |
|---|---|
| Headless cannot prompt: a tool that needs a permission the configured policy does not grant is **auto-denied**, yet the turn still reports `status: SUCCESS` and exits 0 | `mapping.ResultFields.is_error` is true when `denied_actions` is non-empty; `TurnCompleted.permission_denials` carries the entries. A caller that only reads `status` records a silent no-op as success |

The shape that makes this a silent failure, captured from `agy` 1.4.2 with a
`request-review` profile asked to run `echo hi > file`:

```json
{"event":"result","result":{"conversation_id":"…","status":"SUCCESS","response":"",
 "num_turns":1,"usage":{"input_tokens":11907,"output_tokens":71,"thinking_tokens":0,
 "cache_read_tokens":0,"total_tokens":11978},
 "denied_actions":[{"action":"command","display_name":"RunCommand"}]}}
```

Nothing ran, the exit code is 0, and `denied_actions` is the only structured
signal. The adapter turns that into `subtype="denied"`, `is_error=True`,
`permission_denials=("command:RunCommand",)`.
| Every `run_command` is a fresh shell: `cd` and `export` do not survive into the next call | `handoff_rules.SHELL_SEMANTICS`, appended to the handoff by `build_handoff()` |
| A background process started with `&` is killed when the turn ends | same: the handoff tells the agent to use `setsid nohup …` or tmux |

The first is enforced in code and pinned by `test_denied.py`; the other two are
behaviour of the CLI that the adapter cannot change, so they are disclosed in the
text the agent reads, and `test_handoff.py` pins that the text is present (and
absent from a plain `Handoff`).

## Modules

| Module | Owns |
|---|---|
| `transport.py` | `AgyTransport`: spawn, NDJSON framing, SIGINT with process assertions, stderr tail |
| `session.py` | `AgySession`: ordered inbound queue, one event stream, deadlines, totals, respawn after interrupt |
| `mapping.py` | pure `init` / `step_update` / `result` → event translation |
| `handoff_rules.py` | `build_handoff()` and the two shell-semantics rules |
| `config.py` | `AntigravityConfig` and the argv it builds |

## Config bounds

| Field | Default | Reason |
|---|---|---|
| `turn_timeout_seconds` | 900 | a turn that outlives it is interrupted, never silently dropped |
| `cancel_settle_seconds` | 5 | how long an interrupt waits for the CLI and its children before escalating to SIGTERM/SIGKILL and reporting residue |
| `max_pending_turns` | 32 | the queue refuses instead of buffering without bound |
| `max_buffered_events` | 256 | text deltas are droppable under lag; control events never are |
| `dedupe_window` | 256 | how many completed keys stay replayable |
| `skip_permissions` | `False` | headless cannot prompt; a command outside the configured policy is denied, which the adapter reports as failure. Opting in trades that denial for auto-approval |

## Interrupt, measured on the real CLI

`SIGINT` stops `agy` but not its tool tree. In a live run the turn was asked to
`run_command: sleep 90`; after the interrupt the CLI had exited, while both
`bash -c sleep 90` and `sleep 90` were still running. `interrupt()` therefore
reports `settled=False` with those pids in `remaining` instead of claiming a
clean cancel — the assertion is the contract, not a formality. Killing the
residue is the caller's decision; the adapter never widens its own authority.

The session itself recovers: the next turn respawns `agy` with
`--conversation <id>` and completed normally in the same measurement.

## Usage and cost

`agy` reports `input_tokens`, `output_tokens`, `thinking_tokens` and
`cache_read_tokens`; thinking is billed as output, so it is folded into
`Usage.output_tokens` to keep the figure comparable with the other harnesses.
The CLI reports no price, so `cost_usd` stays `None` and is not accumulated —
an unknown cost is not the same as a zero one.

## Tests

`tests/fake_agy.py` is spawned exactly like the CLI and speaks the same framing,
including the SIGINT exit path and a real child process, so the cancel assertions
run against a real process tree. The suite pins the seven shared invariants plus
the three differences above.
