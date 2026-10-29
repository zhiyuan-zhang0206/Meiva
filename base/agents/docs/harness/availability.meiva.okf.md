---
type: doc
title: Harness Availability
description: Local CLI authentication evidence and explicit per-machine refresh snapshots for the spawn form.
tags: [base, agents, frontend]
---

# Harness Availability

## Local probe

`meiva.agents.harness_availability()` reports one row per harness for this machine.
A row carries a status — `available` (installed, and the CLI or the store shows a
live sign-in), `unavailable` (not installed, no sign-in, or credentials that
cannot renew and have expired) or `unknown` (installed, but no login state could
be read) — plus a readable reason, the executable a session would start and
whether PATH led to it.

The probe asks about a stored sign-in; it does not authenticate. Two distinctions
follow. A store is evidence, an account record is not: `~/.claude.json` keeps
`oauthAccount` after the login behind it dies, so a probe reading it alone called
an ubuntu machine signed in while its CLI reported `loggedIn: false`; the record
now reads `unknown`. And a recorded expiry proves a logout only where the
credential cannot renew itself — Claude Code's refresh token does, its access
token is renewed from it, and Antigravity records a one-hour access token plus
the refresh token that replaces it. A past access-token expiry only schedules the
next renewal, so it is reported as age. Where the store records no expiry at all
(Codex keeps opaque tokens), the token is the evidence and the reason its age.

The store has to belong to the CLI: Antigravity's IDE writes
`~/.gemini/jetski-standalone-oauth-token`, the CLI reads only its own
`antigravity-cli/antigravity-oauth-token`, and an IDE-only machine answers
`authentication required` — so only the CLI's file is read.

The probe asks the CLI, then reads the store. It runs `claude auth status --json`,
`codex login status` or `agy models` — the one that belongs to that harness —
through the environment a session would be handed and under a bounded timeout,
and falls back to the stored credential only when the CLI cannot answer: a
timeout, a binary that will not run, or output that names no login state. Neither
path starts a session or spends model quota, and a sign-in nobody can prove stays
`unknown` rather than an invented answer. When PATH misses a user-level install
the probe still looks in the conventional bin directories, because an agent
process starts without the interactive shell profile; `on_path=false` says that
is how the executable was found.

Recording and validating the harness is one slice: the launchers that actually
start a session on an external harness are separate, and they read this column
rather than re-deriving the choice.

## Choosing a harness for a new agent

The spawn form reads `GET /api/cluster/machines/{name}/harnesses` for the
machine that would run the agent. This only reads the last explicit check; an
unchecked machine returns an empty list. Page loads and picker opens do not
probe binaries or login stores. The gateway retains successful snapshots for its
process lifetime; a gateway restart leaves them unchecked again.

The Refresh button above Spawn calls
`POST /api/cluster/machines/{name}/harnesses/refresh` for the selected machine.
Only that endpoint dispatches `harness_probe` to its runner, which answers from
`probe_local_harnesses()`. Concurrent refresh requests for one machine share the
same in-flight probe, owned by the leading HTTP request. Cancelling a follower
does not cancel that work; cancelling the leader settles all followers without
replacing the snapshot. A failed refresh preserves the prior snapshot and is
reported to the caller. A name no machine carries answers 404, and a machine
without an agent-runner answers 503. Unchecked never means not installed.

Each row carries the probe's `status` and `reason` unchanged, plus `launchable`,
which the gateway composes from two facts: a launcher for that harness exists in
this build (`LAUNCHABLE_HARNESSES`) and the machine can actually run it
(`available`, or the built-in runtime, which needs no CLI). A harness can
therefore be installed and signed in and still not launchable. The form keeps
such a row visible and disabled; creating an agent the machine cannot start
would only look like success.
