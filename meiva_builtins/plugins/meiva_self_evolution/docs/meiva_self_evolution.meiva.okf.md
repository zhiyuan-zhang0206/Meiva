---
type: doc
title: meiva_self_evolution — the lesson-record reminder
description: What the builtin meiva_self_evolution plugin adds at the compaction checkpoint — one conditional note asking the agent to record a lesson in its workspace's to_do_better.md — why that note is not a per-window obligation, and how the file is seeded and kept.
tags:
- extensions
- plugin
- agent-extension
---

# meiva_self_evolution — the lesson-record reminder

Agents learn something durable in the last stretch of a context window — a wrong
assumption they corrected, a mistake they recovered from, a correction from the
user — and the soft-compaction checkpoint replaces that history with a summary.
This plugin adds one paragraph to that checkpoint's reminder telling the agent it
may keep such a lesson in a file that outlives the window.

## The note is conditional, not an obligation

The reminder asks for a lesson **only when one exists**. A window that produced
nothing worth keeping writes nothing, and leaving `to_do_better.md` unchanged is
not a missed duty, a failed check or a reason to invent an entry. The text also
forbids restating status, results or the summary: those already live in the
summary being written, and duplicating them would turn the file into a second,
worse summary.

## Where the text appears

`agent_runtime.self_evolution_compact_reminder` is a compact-reminder section.
The kernel collects the enabled plugins' sections in
`agent/graph/hooks/compact.py::_reminder_content` and renders each under a
`[<plugin>]` heading, after the standing wind-down note and the to-user
paragraph. A section that returns the empty string contributes nothing, which is
also the answer when `agent.workspace_in_system_prompt` is false (the bench
runner): the workspace path is not in that agent's prompt, so naming the file
would point at something the agent cannot see.

## The file

`to_do_better.md` sits in the agent's own workspace beside `to_user.md`, and it is
not the user-facing panel: the panel states current results and decisions for
the user, while this file records the agent's own lesson — what went wrong and
the specific change to make next time. Nothing renders it; the agent reads it on
its next pass over its workspace.

The file is seeded empty by the plugin's `after_init` hook
(`ensure_to_do_better`) on the first boot of a process that carries an agent
identity, so the path in the reminder already exists when the agent appends to
it. Seeding creates only a missing file: an existing record is never rewritten,
truncated or moved, so a lesson from an earlier session survives every restart.

## Removability

Disabling the plugin removes the paragraph and the seeding together. No other
code reads the file, and the kernel's reminder keeps its standing note and its
to-user paragraph unchanged.
