---
type: doc
title: Command package notes
description: 'Cross-cutting notes on the command packages: probes, plans and shells, and how the verbs share them.'
tags:
- cli
- commands
---

# Command package notes

## Notes

- Gate is an ordinary gateway service selected into the root manifest. Planned
  root downtime includes its entry port; it has no separate OS job or detached
  launcher. See [[services/entrypoints/gate/docs/gate.meiva.okf.md|Fleet UI Gate]].

- `agents/timeline.py` exposes the existing timeline API as `meiva agents timeline`
  and its exact `context` alias. `agents/impersonation.py` manages explicit external
  requests, leases, inbox acknowledgments, local Python SDK attachment, and the
  one attested send (`send` — task #4102).
  `agents/impersonation_relay.py` forwards inbound availability to the owning external
  model session; `--codex-remote` routes to the app server holding a Codex thread
  without waiting for its external queue-store scan.
  Usage: [External agent impersonation](../../../docs/conventions/agents/agent-impersonation.md).

- Which cluster a command acts on comes from `cli/commands/_repo.py:repo_root` — the
  checkout the running `meiva` belongs to — never the current directory.
- What `meiva start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in
  [[cli/commands/lifecycle/docs/start-readiness.meiva.okf.md]].
- A fleet update is `python -m cli.fleet_update run` (`cli/fleet_update.py`):
  one invocation stops, switches, starts and verifies all listed units. Separate
  `down` and `up` commands support manual release work and recovery; see
  [[cli/docs/fleet-update.meiva.okf.md]].
- Host-level Application Firewall and Redis bridge wiring are one subject:
  [[cli/commands/converge/docs/converge-host-wiring.meiva.okf.md]].
- Converge-managed copies and their drift policy:
  [[cli/commands/converge/docs/rendered-files.meiva.okf.md]].
- Explicit editable-install inspection and write-window primitives are described
  in [[editable-install-guard.meiva.okf.md]]. Ordinary start and converge never
  repair or reinstall a separate production virtualenv.
- `cli/init_intent.py` and `cli/start_intent.py` are routed **before** settings-gated
  imports in `main()`: `meiva init` records complete home identity (and starts nothing),
  and `meiva start` admits that identity, before runtime configuration loads.
- [[cli/commands/converge/docs/ownership_preflight.meiva.okf.md]] names the
  warning-only ownership repair guard that runs before converge writes later
  host state.
