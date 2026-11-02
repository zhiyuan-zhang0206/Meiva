---
type: doc
title: Fixed-Name Registration Audit
description: The four remaining sites that register a module under a fixed name in `sys.modules` before executing it, audited against the Pydantic namespace defect fixed in the two plugin-face readers — which of them can overlap in one process, which execute a module that could reach the defect, and why none is locked.
tags:
- plugins
---

# Fixed-Name Registration Audit

PR #8164 (task #6003) fixed `config_face._read` and `schedule_face._load_face_module`:
both execute a plugin face after registering it in `sys.modules` under a fixed name,
and two overlapping executions of one face left the earlier Pydantic model resolving
its annotations against the later execution's half-built module
(`PydanticUserError: <Model> is not fully defined`). That PR listed four further
sites of the same shape and left them open; this page is the audit behind keeping
them unlocked. The two fixed readers are re-entrant-locked, so a face read can
re-enter its own window from another thread.

## The criterion

A site is the same defect only when both hold:

1. two threads of one process can be inside the function at the same time; and
2. the module it executes can declare a Pydantic model that needs a **deferred
   rebuild** — an annotation naming a class of the same module under
   `from __future__ import annotations`, resolved through
   `sys.modules[cls.__module__]` on first instantiation rather than at class
   creation.

One of the two without the other is recorded at the site; neither earns a lock.
Locking every loader was refused: it would serialize plugin loading for a race no
caller can currently cause.

## The four sites

| Site | Concurrent entry | Module with a sibling-reference model | Conclusion |
|---|---|---|---|
| `base/packages/plugins/loading/plugin_loader.py:64` `load_plugin_module` | none in tree: SDK load, agent-host boot, CLI catalog — all once per process, on the process's own thread | no: repo plugin faces declare none (state models live in the `_state` sibling, imported by importlib under its own lock) | only (2) could hold later; no lock |
| `base/packages/plugin_providers.py:22` `_load_one` | none in tree: catalog built once per process, request paths read `app.state.catalog` | no: no `provider.py` declares a model; they return `PluginContributions` | only (2) could hold later; no lock |
| `services/supervision/roster/plugin_services.py:104` `load_plugin_module` | none in tree: `build_services` at root wiring and in single-threaded CLI commands | no: `services.py` declares `ServiceSpec` dataclasses | neither today; the name cannot vary, so a lock is the shape if (1) ever holds |
| `base/packages/dashboard_supply.py:100` `_import_plugin_metrics` | none in tree: the supplier runs in the CLI render / converge process only | no: `metrics.py` declares `MetricSpec` values | neither today; a server-side supplier would need the lock |

### Why the entries are single-threaded

- **`load_plugin_module`** — reached through `base/packages/plugins/loading/faces.py:84`
  `import_faces`, which `meiva/sdk_surface/extensions.py:56` `load_extensions` drives.
  Its production callers are `meiva/boot.py:527` `load_surface` (the SDK load of one
  process — under the import lock at `import meiva`, otherwise through the load-attempt
  marker), `services/agent_runner/agent_host/lifecycle/configuration.py:91`
  `load_installation` (host boot, before it serves), and `agent/extensions/catalog.py:289`
  `build_catalog` (reached only from `meiva plugins …`; `cli/commands/extensions/parsers/plugins.py:69`).
  No op handler, gateway request or turn path loads a plugin: the ops dispatcher's own
  `config_read` / `config_write` reach `config_face`, not this loader. Of two overlapping loads
  that a new entry could cause, the second either binds a second module object (both passed the
  in-place check before either registered) or re-executes the registered one from two threads;
  serializing the new caller, not this loader, is the fix either way.
- **`_load_one`** — reached from `base/packages/plugin_providers.py:77`
  `build_model_catalog`, called by `meiva/boot.py:484` (`_load_inputs`, the SDK load),
  `services/agent_runner/agent_host/lifecycle/configuration.py:113` (host boot) and
  `gateway/app.py:703` / `:740` (app construction; `load_model_catalog` runs the build
  once, then requests read `request.app.state.catalog`).
- **`load_plugin_module` (roster)** — reached from
  `services/supervision/roster/composition.py:470` inside `build_services`, whose callers are
  `services/supervision/meiva_root_glue/glue.py:375` (root wiring, once at boot),
  `services/supervision/roster/manifests.py:127`, `services/supervision/roster/spec.py:234`
  and the CLI (`meiva status` / `start` / `stop` / `converge`, one command per process).
  Root health monitoring probes the roster `build_services` returned; it does not rebuild
  it per probe. The `healthcheck_module` a plugin declares is documentation-only — `ServiceSpec`
  says so, and nothing resolves it by name — so the fixed name is required for the module to stay
  registrable, not for a monitor to import it.
- **`_import_plugin_metrics`** — reached from `base/packages/dashboard_supply.py:229`
  `render_dashboard_json`, whose callers are `cli/commands/observability/grafana_render.py:114`
  and `cli/commands/observability/_lgtm_provisioning.py:55`.

### Why no executed module reaches (2)

Across the repository, 47 modules declare a Pydantic model whose annotation names a
class of the same module — the deferred-rebuild shape — and the two plugin
`default_config.py` faces among them are the ones #8164 fixed. None is a module any
of these four loaders executes. The plugin state models are the nearest case and are
out of reach twice over: they live in a `_state` sibling that `importlib` imports
under its own per-name lock, and their annotations name imported types
(`AnyMessage`, `add_messages`), not siblings.

A third-party plugin can still ship a face that declares one — that is the trigger
recorded on each site, and it alone does not create the race.

## The one unlocked window

Sites 1 and 2 share a caller that is not thread-safe: `meiva/boot.py:333`
`ensure_plugins_loaded` reads the installation slot (`_slot_facts`), plans
(`meiva/sdk_surface/load_plan.py:64` `plan_load`) and only then marks the attempt
(`meiva/boot.py:371`), so two threads that arrive before the mark both plan a load and
run `_load_inputs` (catalog, site 2) and `load_surface` (faces, site 1) concurrently.
No in-tree caller does that: the import-time path holds the import lock, the schedule
runner and the agent host load once on their own thread, and the lazy backstops
(`meiva/__init__.py` `__getattr__`, `agent/execution/child.py:238`, the harness
attachment's `_prepare`) only fire for a process that has not loaded yet.

The SDK load is therefore where a second concurrent entry would first bite, and where
a fix belongs if one ever appears — one lock around the read-then-mark, not a lock in
each loader.

## See also

- [[docs/plugins/module-loading/module-loading.meiva.okf.md]] — the loader contract these sites belong to.
- [[docs/plugins/module-loading/reload-semantics.meiva.okf.md]] — the reuse-in-place identity that rules out a per-execution name at site 1.
