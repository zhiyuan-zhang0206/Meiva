---
type: doc
title: Launchable harnesses and the birth gate
description: Why a birth on a harness no launcher serves is refused, which two run-time gates keep a pre-existing row inert, and what "launchable" does not promise.
tags: [base, agents]
---

# Launchable harnesses and the birth gate

Split out of [[base/agents/docs/harness/harness.meiva.okf.md|Agent harness identity]],
which owns the value set, its immutability and the fork rule. This node owns what
happens when the release has no launcher for a stored harness: the birth refusal
and the gates that keep an older row inert.

## No launcher, no birth

Recording a value nobody can honour would make the column a claim about a runtime
loop that is not running — and because the value never changes, the agent could
be neither run nor moved onto another loop. `LAUNCHABLE_HARNESSES` names the
harnesses a launcher exists for, and `resolve_birth_harness` refuses the birth
itself for anything else:
`HarnessNotLaunchable` (`harness_not_launchable`, HTTP 400) names the harness and
the launchable ones, and the transaction rolls back before the row exists. A fork
inherits its source's harness, so a source born before this rule — or one whose
launcher was retired — is refused by the same check instead of copying a value
nothing can start.

## Rows born before the rule

Membership is checked again at run time, which is what keeps pre-existing rows
inert instead of quietly running the built-in loop under an external label. Two
gates refuse to run anything outside the set:

- `validate_launch_row` (`ops/lifecycle/launch.py`) refuses the spawn's launch op
  with a reason naming the harness, so the caller sees why;
- `is_runnable` (`services/agent_runner/agent_host/scheduling/wake_screening.py`)
  refuses the hosted turn itself. The launch op is only one way into a turn: the
  delivery watchdog, the durable pending scan and pub/sub wakes all reach
  `_run_turn` without it, and every one of them passes this gate.

Such a row keeps its stored value and stays inert: no turn starts on it, so no
built-in loop runs under an external label. Adding a launcher back means adding
its harness to the set, and `retry_launch` can then wake the rows it serves.
Ending one in the meantime needs a force terminate — a graceful terminate is
applied inside a turn the row cannot get.

A launchable harness is not a working one: the wake gate says the release has a
launcher, and the session itself is what fails first if the CLI or its SDK is
missing (`agent/graph/docs/harness-turn.meiva.okf.md`).
