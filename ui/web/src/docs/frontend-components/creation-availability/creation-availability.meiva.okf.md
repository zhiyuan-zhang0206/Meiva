---
type: doc
title: Creation Availability
description: Spawn target gating, agent availability labels, and creation receipt wording in the frontend.
tags:
- frontend
---

# Creation Availability

Creation lives behind the sidebar header's `+`. The form lists agents only on
online, non-paused agent-runner machines with `agent_host_online=true`; the host
verdict keeps its pidfile meaning, and a false or absent verdict leaves that row
disabled with its reason.

Creation is one table, read left to right: **Machine, Harness, Model, Effort**.
The machine comes first: harness availability belongs to a machine.
Every agent-runner is a row; down hosts stay disabled with the reason in a title.
Each column is sized by its content; the panel follows it.
Each column narrows the next, selecting one row on entry.
The harness column speaks for the selected
machine, and a stored or picked harness that machine cannot start falls through
to the first one it can (the pick itself is left alone, so switching back
restores it). The model column keeps only the models whose published
`harnesses` name the chosen harness (`GET /api/models`, `ModelInfo.harnesses`;
the built-in `meiva` runtime runs every spawnable model and is always listed
first), ordered by provider without repeating provider headings above the model
names, excluding superseded catalog entries.
Hidden models and providers filter native API execution only; external CLI
choices remain selectable without changing those preferences.
Which harness runs which model is the registry's declaration, not a frontend
table. Model and Effort select actual execution settings in both layouts.
Native uses the model's `reasoning_effort_options` and
`reasoning_effort_default`; external harnesses use that harness's entry in
`harness_settings`. An unsupported stored effort falls back to the selected
harness binding's concrete default, never the native model default. External harnesses expose no history-model or summary-effort settings: their
CLI owns context and compaction, and Meiva retains a basic timeline projection.
The action rail puts `Refresh` immediately above `Spawn`. Spawn is disabled with
the first refusal that applies (a down host, then a harness the machine cannot
start).

Below the md breakpoint the same decisions stack instead of a table: on a
390px phone the four fixed columns left the model column 4px wide.
Each decision becomes a 44px row that states the current pick and opens its list
on a tap, and a refusal is written beside the action: a touch screen has no
hover.

The table shows the **last spawn, not the last click**. `behavior.spawn_*`
holds the config the previous spawn actually used, cascade included; a click
while the form is open only lays a pick over it, written back when a spawn is
issued and dropped when the form closes without one. Browsing therefore never
edits the stored config, and reopening always answers "what did the last agent
run on". Stored effort is re-derived against the current model's ladder, so an
unavailable level is never sent; an explicit model pick is always sent as spawn
config, even when it equals the cluster default. The backend rejects an
unsupported explicit effort rather than translating it to another grade.

Catalog grouping, rates, defaults, and Control visibility switches are owned by
[[ui/web/src/docs/frontend-components/creation-availability/model-presentation.meiva.okf.md|Model Presentation]].

Guide, schedule, and package-draft creation toasts say "created" and
point to the conversation for progress; they do not claim that a turn started.
The conversation does not render a host-admission or launch-availability strip.

A launch-failure reason is still represented by the roster's launch-failed badge.
A structured create 502 selects its committed `agent_id` and refreshes
roster/detail instead of inviting another create.

Creation HTTP calls send one operation key with the `principal-v1` scope. Spawn/fork mutations hold it in their variables; fork placement and prompt are resolved before mutation admission. Explicit API callers can reuse a key with an unchanged body. A new action mints another key. This does not enable automatic ambiguous retries or a browser outbox.

## Harness choice

A spawn names its harness (`meiva` by default, or an external CLI). It is a birth attribute: the
row records it once and a fork inherits its source's value, so the fork dialog
says so instead of offering a choice the backend would refuse with
`fork_harness_mismatch`.

Opening the picker reads the last snapshot from
`GET /api/cluster/machines/{name}/harnesses`; it never starts a probe. The sidebar
has no page-load warm-up. `Refresh` explicitly calls
`POST /api/cluster/machines/{name}/harnesses/refresh` across all ready runner
machines at once, with a shared pending state and repeat clicks disabled. Each
machine's successful result updates its query cache independently; a failure
preserves that machine's prior snapshot and shows a retryable error. An empty
snapshot asks the user to refresh. The gateway retains snapshots until its next explicit check or
restart, and coalesces concurrent checks per machine. The wire row carries `status` (`available` / `unavailable` /
`unknown`), the probe's `reason`, and `launchable`; the row on screen carries one
word. `available` when `launchable`, `unavailable` otherwise, and the row is
disabled in the second case. `unknown` and `unavailable` are one thing to the
user — "you cannot pick this" — so the picker never shows the word `unknown`, a
harness the machine has installed but this build cannot start reads `unavailable`
like a missing one, and the probe's diagnostic sentence never reaches the
interface. Those stay on the wire: an agent asked "why can't I use codex here"
reads them from `meiva.agents.harness_availability()` instead of the UI.

The same rule covers the spawn target list: a machine that cannot start the
chosen harness has its row disabled with `unavailable`, no reason text.

The chosen harness is a `behavior.spawn_harness` preference. The built-in
`meiva` value is omitted from the request, because it is the server's own
default; any other value is sent as `harness` and the backend's refusal (422
validation, or a `fork_harness_mismatch` reason) is shown verbatim.
