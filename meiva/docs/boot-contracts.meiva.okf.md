---
type: doc
title: Boot contracts
description: Current implementation constraints and their executable evidence.
---

# Boot contracts

Parent: [[meiva/docs/meiva.meiva.okf.md]].

The installation retains its own cold clients until process exit; a bound attachment uses its own clients. Identity binding precedes plugin installation. Surface loading and later runtime-face loading each retain their attempted/failed state: unknown configuration, inventory and I/O failures propagate without retry, while typed declaration refusals are isolated after rollback. An importlib fromlist probe cannot start loading against a half-imported SDK; the entry completion flag and in-flight loader slot are separate guards. Missing private names never start a load. A process carrying a harness registration handle borrows that identity on its first identity read rather than at import — the attach replays the agent's plugin-state snapshot, which opens the database — and a failed borrow is recorded for the read that needed it instead of escaping the import.

## Facade and implementation

`meiva/boot.py:ensure_plugins_loaded` is the facade that reads the process (the installation slot, the bound context, the launch environment) and builds facts; `meiva/sdk_surface/pure/load_plan.py:plan_load` chooses the step (raise the recorded failure, bind the catalog, upgrade the faces, load the surface), so the order of those gates is a table, not a process. Every step except the surface load is carried out by `meiva/boot.py:settle_installed` from the step and the context alone, and the `match` over the steps names each member and ends in `assert_never`. Only a load plan resolves the owners a surface load needs into `meiva/boot.py:LoadInputs` (disable entries, configuration, machine, clients, sampling owner, clock, home, model catalog) and `meiva/boot.py:load_surface` installs them. A failure while resolving or installing is recorded in the slot and re-raised as the same exception on every later call.

`meiva/boot.py:process_clients` requires every owner the clients share: configuration, machine, database factory and gateway endpoint. `derived_clients` is the root whose database and endpoint come from the configuration and the machine, and `gateway_endpoint` is the endpoint resolved on first use. The MCP client set reads its request timeout from the configuration once, when the first MCP call builds it. The installation's sampling owner is never defaulted: `install` and a first `load_extensions` require it, and `meiva/boot.py:process_sampling_owner` is the root that closes one at interpreter exit for a process with no service lifespan.

An external process boots one of two values. `meiva/boot.py:borrow_external_process` makes a `BorrowedBoot` (the machine and database factory of the context it borrows) and `own_external_process` an `OwnedBoot` (configuration, machine, database factory, clients, clock); consumers `match` on the two and `initialize_external` brings the plugin surface up around either. The takeover and the harness attachments receive their process facts as values (`AttachInputs`, `HarnessInputs`), read once by their facade; the harness shell keeps asking its clock for the instant at every SDK call because expiry is rechecked then. Tests build `Installation.bare(sampling=...)` and hold it with `meiva/sdk_surface/tests/installation_holder.py:held_installation`; they do not replace the slot.

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
