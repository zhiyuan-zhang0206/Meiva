---
type: doc
title: Harness turn capability boundary
description: Which lifecycle and context operations stay native-only (`Harness.MEIVA`) versus what external harness sessions support on `meiva.self` and `/api/agents`.
tags:
- agent
- graph
- external-agents
---

# Harness turn capability boundary

Part of [[agent/graph/docs/harness-turn.meiva.okf.md|Harness turn]].

Because an external harness CLI owns its context window and turn loop, native
context/lifecycle mutations are native (`Harness.MEIVA`) only:

- **`fork`, `compact`, and `meiva.self.attach` are native-only**:
  - `fork` (`fork_from`) and `POST /api/agents/{id}/compact` reject external harnesses with HTTP 400 (`ForkHarnessMismatch` on fork; guarded in `claim` dispatch); the web UI hides `Fork` and `Compact` (and omits `/compact` from slash autocomplete) for external harnesses.
  - `POST /api/agents/{id}/restart` and the Web UI `Restart` action are supported on `idling` external-harness agents and for bidirectional harness switching on `idling`/`terminated` agents (requiring a non-empty `message`). Calling `meiva.self.restart()` from code remains native-only (raises `RuntimeError` on external harnesses).
  - Calling `meiva.self.compact()` or `meiva.self.attach()` on an external harness raises `RuntimeError`.
- **Supported `meiva.self` surface on external harnesses**:
  - `harness_sdk_expand_section` and `meiva.help(meiva.self)` expose only `meiva.self.terminate()`, `meiva.self.pause_heartbeat(duration)`, and read-only identity constants (`AGENT_ID`, `MACHINE_SPEC`, `MachineSpec`). Use `terminate` and `resurrect`, or `restart` via API/UI to stop and resume an external harness agent.

A cancelled turn keeps its message in the window, so the next turn re-submits it. `LAUNCHABLE_HARNESSES` gates a wake before any of this runs.
