---
type: doc
title: Creation Availability
description: Spawn target gating, agent availability labels, and creation receipt wording in the frontend.
tags:
- frontend
---

# Creation Availability

`SpawnButton` reads `/api/status` and places agents only on online,
non-paused agent-runner machines with `agent_host_online=true`. The host
verdict keeps its existing pidfile meaning. A false or absent verdict leaves
the runner disabled with an explicit reason; when another runner is ready, the
blocked runner remains a disabled picker entry. With one eligible ready
runner, the button sends directly; with multiple eligible runners it opens a
popover. The disabled trigger's tooltip sits on a wrapper so it still appears
when the button itself cannot receive pointer events.

Creation is one table, not three dropdowns side by side: Harness, then the
models that harness runs, then the efforts that model publishes. Each column
narrows the next and marks exactly one row selected as soon as the form
renders, so opening it answers "what will the next agent be born on" before it
asks anything. The model column keeps only the providers the chosen harness can
start — the built-in `meiva` runtime keeps every provider — grouped under a
vendor heading, with hidden and superseded catalog entries excluded. The effort
column shows the selected model's `reasoning_effort_options` ladder; a model
that publishes none leaves the column empty rather than offering a synthetic
default, and the model's concrete `reasoning_effort_default` is the row marked
when nothing is stored. The column is titled "Effort": the header names the
quantity, not the mechanism.

The table shows the **last spawn, not the last click**. `behavior.spawn_*`
holds the config the previous spawn actually used, cascade included; a click
while the form is open only lays a pick over it, written back when a spawn is
issued and dropped when the form closes without one. Browsing therefore never
edits the stored config, and reopening always answers "what did the last agent
run on". Stored effort is re-derived against the current model's ladder, so an
unavailable level is never sent; an explicit model pick is always sent as spawn
config, even when it equals the cluster default. The backend rejects an
unsupported explicit effort rather than translating it to another grade.

The model row keeps the input rate visible — comparing models at a glance is
what the column is for — and carries the rest on hover: every rate behind that
price (input, cache read, output), the declared 5-minute and 1-hour cache-write
rates, and the vendor reference TPS with its source and check date. Prices are
USD per million tokens. Standard and Fast service IDs have separate catalog
entries and prices. Absent reliable absolute vendor figures say so instead of
showing a blank column; relative speed multipliers and cluster measurements
never stand in for the vendor figure.

Declared `fast_of` services appear directly after their visible Standard model.
Families sort by the Standard input price; a service whose Standard model is
hidden remains an independent row sorted by its own price. Names do not imply
family membership, and grouping never restores hidden or superseded entries.

The published default uses the same database/config resolution as a new
agent's birth stamp. Saving the cluster default invalidates both default-model
and model-catalog queries, including the otherwise indefinitely cached picker.

Rates below USD 1 use up to four significant digits, with at least two decimal
places, so nonzero cache prices such as USD 0.003 never display as USD 0.00.
The price tooltip labels input/cache-read/output, explains that the displayed
rates cover the current base tier, and includes declared 5-minute/1-hour
cache-write rates. Longer contexts and recurring rate windows can select
different rates for actual billing.

Guide, schedule, and package-draft creation toasts say "created" and
point to the conversation for progress; they do not claim that a turn started.
The conversation does not render a host-admission or launch-availability strip.

A launch-failure reason is still represented by the roster's launch-failed badge.
A structured create 502 selects its committed `agent_id` and refreshes
roster/detail instead of inviting another create.

Creation HTTP calls send one operation key with the `principal-v1` scope. Spawn/fork mutations hold it in their variables; fork placement and prompt are resolved before mutation admission. Explicit API callers can reuse a key with an unchanged body. A new action mints another key. This does not enable automatic ambiguous retries or a browser outbox.

## Harness choice

A spawn names its harness (`meiva` by default; `claude_code`, `codex`,
`antigravity` for the external CLIs). It is a birth attribute: the row records
it once and a fork inherits its source's value, so the fork dialog says so
instead of offering a choice the backend would refuse with
`fork_harness_mismatch`.

The picker reads `GET /api/cluster/machines/{name}/harnesses` for the machine
the agent would be placed on — the probe runs where the runtime would run, not on
the gateway. The wire row carries `status` (`available` / `unavailable` /
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
