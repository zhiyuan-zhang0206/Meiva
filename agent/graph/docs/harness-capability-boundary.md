# Harness turn capability boundary

Part of [Harness turn](harness-turn.md).

Because an external harness CLI owns its context window and turn loop, native
context/lifecycle mutations are native (`Harness.MEIVA`) only:

- **`fork`, `compact`, and `meiva.self.attach` are native-only**:
  - `fork` (`fork_from`) and `POST /api/agents/{id}/compact` reject external harnesses with HTTP 400 (`ForkHarnessMismatch` on fork; guarded in `claim` dispatch); the web UI hides `Fork` and `Compact` (and omits `/compact` from slash autocomplete) for external harnesses.
  - `POST /api/agents/{id}/restart` and the Web UI `Restart` action are supported on `idling` external-harness agents and for bidirectional harness switching on `idling`/`terminated` agents (requiring a non-empty `message`); a running agent is answered 409.
  - `meiva.self.restart()` works on every harness and is the one road for a running agent: the request is validated by the same gateway route (`at_turn_end`, the agent's own source only), recorded as a pending `restart` command and applied by the host when the turn ends, never during it
    ([decision](../../../docs/decisions/agents/graph/2026-11-11-self-restart-at-turn-end.md)). On an external harness the call returns a receipt and the agent ends its own turn; the harness CLI owns the turn, so nothing interrupts it.
  - Calling `meiva.self.compact()` or `meiva.self.attach()` on an external harness raises `RuntimeError`.
- **Supported `meiva.self` surface on external harnesses**:
  - `harness_sdk_expand_section` and `meiva.help(meiva.self)` expose `meiva.self.restart(message, config_overlay=None)`, `meiva.self.terminate()`, `meiva.self.pause_heartbeat(duration)`, and read-only identity constants (`AGENT_ID`, `MACHINE_SPEC`, `MachineSpec`).

A cancelled turn keeps its message in the window, so the next turn re-submits it. `LAUNCHABLE_HARNESSES` gates a wake before any of this runs.
