---
name: external-agents
description: "Launches and supervises external coding agents. Use when delegating coding work or resuming a session."
---

# Use other agents

Both `claude` (Anthropic) and `codex` (OpenAI) are coding-agent CLIs you can hand
a task to and let plan + execute. Treat either as "another agent". A session
runs as a **delegated worker** you supervise through files.
For the session primitives themselves see `meiva.shell` (`run` for one-shot;
`sessions` — `new` / `send` / `send_keys` / `capture` / `kill` — for a persistent one)
and `meiva.watcher`; don't re-derive those here.

> **Flags and models drift between releases.** Everything below is a snapshot,
> not a contract. Confirm with `claude --help` / `codex exec --help` and `claude
> --version` / `codex --version` on the actual machine before relying on a flag —
> and note a machine may have only one of the two installed.

## Delegated workers — file-driven collaboration (the pattern for long tasks)

You hand a long task to the tool and keep steering it through the file-driven
pattern — a task file you append to, a work file the tool rewrites, a supervisor
that wakes you. You stay the decider; the tool is a worker.

Use `meiva-workflow` to decide whether delegation serves the task, define its
scope, and set verification criteria. Check the actual tool's installed capabilities
before choosing it.

Read [delegated workers](references/delegated-workers.md) for workspace setup,
contract delivery, supervision, and handoff. Read only the chosen tool's launch
reference.

## After an interruption — resume

A full stop, a reboot, a crash or a TTL expiry closes the shell a coding
session runs in, but the tool keeps its conversation. Every launch prints the
tool's session id (`claude_session=` / `codex_session=` / `antigravity_session=`), and
`--resume <id>` on the same spawn command reopens it. Whether to resume or
start fresh is your call from the task's context: [Resume after an
interruption](references/resume_after_interruption.md).

## CLI reference

Flags drift — confirm with `--help` on the actual machine. The per-tool
references carry the launch variants, auth traps and headless notes:

- [Claude Code (`claude`)](references/claude_code.md)
- [OpenAI Codex (`codex`)](references/codex.md)
- [Google Antigravity (`agy`)](references/antigravity.md)

The spawn picker prints one word — `unavailable` — for three different states.
When someone asks why a harness they use is greyed out, the states, the
self-check order and the false alarms are in
[why a harness shows unavailable](references/harness-availability.md).

A host that runs `meiva start` also copies this guide
into the global skill directory of each external agent surface it finds — CLIs and,
where a desktop app documents one, its skill directory — so a spawned session can
read them without the Meiva checkout. Which directories those are, and the
evidence for each, is [client skill
directories](references/supported-clients.md).
