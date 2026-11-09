# Plugin Module Loading

## One loader contract, two production call sites
`meiva/sdk_surface/extensions.py:load_extensions()` imports every enabled plugin's
`plugin.py` (plus its optional `agent_runtime.py` face on the full form) by
path — at host boot and in the plugin catalog. The
agent-launched child enters through the same loader (`meiva.ensure_plugins_loaded`);
its stateless form loads surfaces only ([Plugin Faces — SDK Surface and Agent Runtime](two-faces.md)).
All drive the same primitives (`base/packages/plugins/loading/`), so a plugin sees the same
module name, `__package__`, `sys.modules` identity, and containment whichever
production path imports it (issue #2161 — before unification the boot loader
exec'd plugins under a top-level name and relative imports crashed the agent
host). Importing a plugin registers nothing: its faces export `contribute()`, and the
loader builds the registry and installs the SDK surface afterwards.

The name given to `importlib.util.spec_from_file_location` is **dotted**
(`meiva_builtins.plugins.<name>.plugin` built-in, `plugins.<name>.plugin`
external), so importlib sets `__package__` and a `from . import x` inside
`plugin.py` resolves. A directory under `base/paths/__init__.py:repo_plugins_dir()`
is built-in; anything else is external.

Load order is the `config.plugins` dict order (alphabetical), one by one —
**no dependency declaration, no topological sort**. Configs are bound by the SDK install (`bind_plugin_config`)
only after every import has completed, so a hook firing later always finds
`meiva.sdk_surface.settings.plugins.<n>` populated.

## Two faces: `plugin.py` surface, `agent_runtime.py` face
A plugin loads in up to two faces, and each load form picks which run: the
child's stateless boot loads surfaces only; the full form (graph build, host
boot, a stateful child's first state use) adds the optional `agent_runtime.py` face
carrying the agent-side registrations (state fields, hooks, prompt sections).
Per-face containment, the face's dotted name, and the load forms in full:
[Plugin Faces — SDK Surface and Agent Runtime](two-faces.md).

## Disabled means never imported
The enable set comes from the per-machine `plugins_config.json`, read by both
loaders through `base/plugins_config`: host boot via `load_for_runtime()`
and the graph loader via `load()`, each falling back to
`load(allow_dangling=True)` when a config entry's plugin directory is gone —
every dangling name is reported on each failed load by the canonical
reporter and treated as disabled. A plugin with
`enabled: false` is imported by neither `plugin.py` loader (issue #2161: the
boot loader imported every directory on disk regardless of config, so `meiva
plugins disable` changed nothing about startup). Host boot passes the enabled
set of the *external* plugins it scans; the graph build adds the built-ins.
Machine-level roster paths sit outside the enable plane: the
`services.py` roster and the shipped-`metrics.py` scan key on presence, not
enable-state (`services/supervision/roster/plugin_services.py:plugin_services`).

## The external `plugins` prefix is registered, not resolved from sys.path
`register_plugin_parent_packages` (`plugins/loading/plugin_loader.py`, applied by the loader for
external plugins) registers `plugins` over `$MEIVA_HOME/plugins` and
`plugins.<name>` over the plugin's directory before executing `plugin.py`. It
does not rely on `$MEIVA_HOME` being on `sys.path`: the exec child boots with
`cwd=$MEIVA_HOME/source` under `python -I`, where `import plugins` resolves to
a legacy checkout `plugins/` dir or to nothing, so `from . import _sibling`
inside `plugin.py` used to raise `ModuleNotFoundError` and take `import meiva`
down with it (2026-08-28 meiva_ledger incident). Existing `sys.modules` entries are left untouched, and
built-ins are excluded: they resolve through the real `meiva_builtins.plugins`
package.

## Fail-soft: a broken plugin is skipped, never fatal

Load failures are contained with a loud report while other plugins continue.
The reporting owner, load sites, cancellation and telemetry failure boundaries
are documented in [Plugin load failure reporting](failure-reporting.md).

## Semantics boundary: what stays fail-closed
Containment covers *code* that fails to load; inventory and contract conflicts
stay hard instead of guessing at the operator's intent (duplicate plugin name,
malformed `plugins_config.json`, config schema drift, provider
registration-contract violations, post-load revalidation) — and the release
probe re-raises the contained failures of the faces it exercises (the
`plugin.py` loader, dangling entries included; the `services.py` roster;
provider registration). See
[Plugin Load Fail-Closed Boundaries](fail-closed-boundaries.md).

## Registration precedes execution
The module object is placed in `sys.modules` **before** `exec_module` runs, not
after. A `BaseModel` defined inside a plugin triggers Pydantic's
`__init_subclass__`, which resolves `Annotated[T, reducer]` string annotations
via `get_type_hints` reading `sys.modules[cls.__module__].__dict__`. With no
entry there, the annotation stays a `ForwardRef`, and the later
`StateGraph(schema)` build raises `NameError` far from the cause.

## Reload semantics
A repeat load re-executes the module object already registered for that file
(`importlib.reload` semantics — module identity stays stable per process), and
reload is not a lifecycle: the reset covers the framework registries only.
In full: [Plugin Load Reload Semantics](reload-semantics.md).

## The four fixed-name registrations outside this contract
A plugin under its real import name (`plugin_loader.load_plugin_module`), a provider
(`plugin_providers._load_one`), the roster's `services.py`, and a shipped `metrics.py`
register a module the same way in a different contract. Which of them can overlap in
one process, and why none of them is locked:
[Fixed-Name Registration Audit](fixed-name-registration-audit.md).

## Key Dependencies
- [Plugin System](../plugins.md) — the injection surfaces the import registers into
- [Agent Graph (LangGraph Execution Graph)](../../../agent/graph/docs/graph.md) — `build_graph()` calls the loader (`load_extensions`) before wiring nodes
- [Extensions](../../../meiva_builtins/docs/extensions.md) — the `SdkWrap` layers a reload re-installs from a pristine core
