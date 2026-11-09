# Command package notes

## Notes

- Gate is an ordinary gateway service selected into the root manifest. Planned
  root downtime includes its entry port; it has no separate OS job or detached
  launcher. See [Fleet UI Gate](../../../services/entrypoints/gate/docs/gate.md).

- `agents/timeline.py` exposes the existing timeline API as `meiva agents timeline`
  and its exact `context` alias.

- Which cluster a command acts on comes from `cli/commands/_repo.py:repo_root` — the
  checkout the running `meiva` belongs to — never the current directory.
- What `meiva start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in
  [Start readiness](../lifecycle/docs/start-readiness.md).
- A fleet update is `meiva update run` (`cli/fleet_update/`):
  one invocation stops, switches, starts and verifies all listed units. Separate
  `down` and `up` commands support manual release work and recovery; see
  [Fleet update over SSH](../../docs/fleet-update.md).
- Host-level Application Firewall and Redis bridge wiring are one subject:
  [Converge host wiring](../converge/docs/converge-host-wiring.md).
- Converge-managed copies and their drift policy:
  [Converge-rendered files](../converge/docs/rendered-files.md).
- Explicit editable-install inspection and write-window primitives are described
  in [Editable Install Guard](editable-install-guard.md). Ordinary start and converge never
  repair or reinstall a separate production virtualenv.
- `cli/init_intent.py` and `cli/start_intent.py` are routed **before** settings-gated
  imports in `main()`: `meiva init` records complete home identity (and starts nothing),
  and `meiva start` admits that identity, before runtime configuration loads.
- [Converge Ownership Preflight](../converge/docs/ownership_preflight.md) names the
  warning-only ownership repair guard that runs before converge writes later
  host state.
