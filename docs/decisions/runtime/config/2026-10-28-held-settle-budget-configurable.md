# Held-path settle budget becomes configuration

## Context

`HANDOFF_SETTLE_RETRY_BUDGET_S = 5.0` (`base/deploy/progress_timeout.py`) is the whole wall-clock
budget the wake that ends a takeover may spend settling its one owed checkpoint write before it
fails closed (#5468). On 2026-10-28 the cluster update produced three `hosted_handoff_settle_failed`
events — 15:48:09 on agent 426, then 16:05:03 on 2697 and 426 — each with `error_class=TimeoutError,
budget_s=5.0` and each seconds after the units came back. Both rows read `idling` with
`last_turn_fatal_at=NULL`: the receipt had landed, so `_fail_closed` took the `hosted_handoff_settle_raced`
branch (a warning, no marker) and nothing needed recovering. The two event names already separate the
benign race from a settle that really failed closed; what the events measured is a five-second budget
meeting a cold pool's first dial and a cross-machine checkpoint read.

The 2026-09-17 ruling asks for two things: behaviour-shaping constants are configurable, and every
limit carries a written reason. The reason already existed in the note above the constant; the knob
did not. The value was a bare literal, consumed as a dataclass default in
`services/agent_runner/agent_host/held_settle.py`, so widening it meant editing and redeploying code.

## Decision

- `settings.daemon.held_settle_budget_s` (`MEIVA_HOST_HELD_SETTLE_BUDGET_S`), defaulting to
  `HANDOFF_SETTLE_RETRY_BUDGET_S` (5.0 s), cluster-pinned, `restart_required: all`, with the reason
  in its description.
- The deploy constant seeds the default and stays the family's single definition; the *running* value
  is the setting. `HeldSettle.budget_s` loses its default, so the host root is the only place that
  decides it.
- The value is checked against its bounds at load and at boot, each where it can be seen: the daemon
  domain refuses a value that reaches the turn's own no-progress bound
  (`MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`), and the host root refuses one that reaches
  `NO_PROGRESS_TIMEOUT_S` (the ops wedge bound) or `CORPSE_REAP_GRACE_S` (how long a crash-marked row
  may sit dead) through the clock lattice `base/deploy/timing.py` already declares. `clock_values()`
  gained the budget as its third operator-set clock and `agent_host/daemon.py` passes the configured
  value, so `assert_clock_lattice` judges what the host will live by rather than the default it was
  seeded from. The same lattice declares the family's floor: the retry pays one interval and then
  three times it before the third attempt, so a budget below that sum (2 s at the family's 0.5 s
  interval) cannot buy the three attempts the budget is declared to pay for and is refused at boot
  too. A configuration that would either outlive a bound or fail to pay for its own attempts
  therefore fails at boot instead of stalling a wake or dying on its third try.
- The retry interval and the attempt count stay constants: they shape the same wake, and the
  operator's question is how long that wake may take.

## Alternatives rejected

- Raise the constant: still a bare literal, still not operator-adjustable, still against the ruling.
- Keep the dataclass default and read settings where convenient: a default is a second owner of a
  number the domain already declares and guards.
- Warn instead of refusing: a budget above the bound that declares the wake dead cannot do what it
  says, and the symptom would surface later as a stall rather than as a refusal.
- Check all three bounds in the configuration domain: `base/config` may not import `base/deploy`
  (that edge closes a cycle around the configuration the deploy family already reads), so the deploy
  orderings are checked where they are declared, at the host root, on the value the host loads.
- Make the interval and attempts configurable too: they only redistribute the same budget.

## Consequences

- Operators set `MEIVA_HOST_HELD_SETTLE_BUDGET_S` in the cluster's configuration authority, which every
  agent runner fetches through `GET /api/bootstrap` (the scope is `cluster-pinned`, like the host_*
  neighbours); a runner picks the value up on its next restart.
- The default behaviour is unchanged at 5.0 s. The guard is new and fires only on a configuration
  that would previously have outlived the bound silently.
- The 2026-10-28 events stay the accurate record of what a five-second budget does after a restart;
  the knob is how an operator widens that without a code change.
