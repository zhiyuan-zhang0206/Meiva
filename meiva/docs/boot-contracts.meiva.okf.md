---
type: doc
title: Boot contracts
description: Current implementation constraints and their executable evidence.
---

# Boot contracts

Parent: [[meiva/docs/meiva.meiva.okf.md]].

The installation retains its own cold clients until process exit; a bound attachment uses its own producer. Identity binding precedes plugin installation. Surface loading and later runtime-face loading each retain their attempted/failed state: unknown configuration, inventory and I/O failures propagate without retry, while typed declaration refusals are isolated after rollback. An importlib fromlist probe cannot start loading against a half-imported SDK; the entry completion flag and in-flight loader slot are separate guards. Missing private names never start a load. A process carrying a harness registration handle borrows that identity on its first identity read rather than at import — the attach replays the agent's plugin-state snapshot, which opens the database — and a failed borrow is recorded for the read that needed it instead of escaping the import.

## Cold-import evidence

The SDK bootstrap and Fleet lazy-import tests execute their own files in fresh
interpreters with `-I -B -X utf8`. Their module-level imports use only the
standard library; the `__main__` entry calls a source-visible probe that inserts
the supplied repository root, imports the actual SDK or Fleet modules, checks
their identities in `sys.modules`, and reports matching prefixes as JSON.
Pytest collection never runs that entry. The parent strips the existing
launch-marker set, requires a zero
exit status, and requires an empty forbidden-module list. This exercises the
real cold import independently of modules already loaded by pytest, while
keeping the imported targets visible to static dependency tooling.

The SDK guards its LM, database, telemetry and connection stacks in
`meiva/tests/bootstrap/test_lazy_lm_import.py` and `test_lazy_boot_import.py`.
`meiva_builtins/plugins/meiva_fleet/tests/test_lazy_plugin_import.py` guards
database and telemetry imports across the plugin, task registry and task-update
modules. These are import-laziness checks, not deployed boot-latency measurements.
