# Agent Hooks

## What it is

Meiva plugin's **graph-edge hook system**—a single hook container node (`before_turn`) provides the extension point in the LangGraph execution graph before each turn (for both native `llm` and external `harness_turn` execution). Plugins register instances of `Hook` subclasses; the framework calls their typed `__call__` in registration order during graph node execution, with reducer arbitration for co-writes.

Together with SDK wraps (a plugin's declared `SdkWrap`, applied by `meiva/sdk_surface/install.py` through `meiva/sdk_surface/wraps.py`), this forms the two-layer plugin system: SDK wraps modify the behavior of the meiva namespace, while `before_turn` hooks intervene in the agent execution pipeline.

## Core Responsibilities

- **`Hook` base class** (`agent/hooks/_registry.py:Hook`, `ABC`): subclasses override the typed `async def __call__(self, state, runtime, config, /) -> dict | None`. The base locks the signature—under pyright strict, incompatible overrides raise `reportIncompatibleMethodOverride`; not overriding leaves an uninstantiable abstract class. Optional `.name` property (defaults to class name) labels co-write conflict messages.
- **Hook declaration**: a plugin's `contribute()` returns `PluginContributions(before_turn=(...))` of `Hook` **instances** (not class, not bare function); nothing is registered at import. The framework's own are `agent/graph/hooks/framework.py:framework_hooks()`
- **Hook execution**: `agent/graph/hooks/framework.py:hooks_at` composes one `(plugin | None, hook)` sequence at `before_turn` (repair first, then plugins, then the remaining framework hooks); every hook receives the same original `(state, runtime, config)` and only returned deltas merge in execution order — no priority system, no intermediate state mutation. Ordering rationale: [Hook Execution](hook-execution.md)
- **Hook points**: `HookName = HookPoint = Literal["before_turn"]` (`base/packages/plugins/extensions.py`)
- **Co-write arbitration**: two hooks writing the same state key are merged by that key's reducer; with no reducer the runner raises `RuntimeError` fail-loud instead of a silent last-wins ([Hook Execution](hook-execution.md))
- **Instance state**: subclasses carry per-hook state/configuration in `__init__` on `self`—declarations hold instances, not bare functions. Built-in `_CompactReminderHook` / `_RepairDanglingToolPairingHook` are module-level singletons that `framework_hooks()` returns.
- **Route override**: If the dict returned by a hook contains `"goto": NodeName`, the container node's default route is overridden; other keys are treated as part of `Command(update=...)`
- **Activation telemetry**: a hook declared by a *plugin* that returns a non-empty dict also emits one `plugin_activation` event naming the keys it wrote (`base/packages/plugins/activation.py`; attribution is the plugin name paired with the hook in the runner's `hooks`). Framework hooks and `None` returns record nothing, so pure observation stays free. This is the runtime half of the registration ledger and philosophy §6's obsolescence gauge — see [Plugin System](../../../docs/plugins/plugins.md)

## Key Dependencies

- [Agent State](../../docs/state.md) — The hook's `state` parameter is the `AgentState` passed by LangGraph; the returned dict goes through standard LangGraph reducer merging; hooks run at the graph-node level and directly receive/return the whole state. A plugin hook reads its own fields with the handle's pure `view(state)` and returns writes through `delta({...})`; `read` / `update` are the exec child's channel (`meiva.state` / `meiva.state_update` exist only there). A hook module imports no `meiva` (`scripts/lint/plugins/no_meiva_in_hooks.py`)
- [Agent Graph (LangGraph Execution Graph)](../../graph/docs/graph.md) — Placement of the `before_turn` hook container node in the graph topology

## Entry Points

- `agent/hooks/_registry.py:Hook` — Base class; subclasses override `__call__`
- `agent/graph/hooks/framework.py:framework_hooks()` — The framework's own hooks at `before_turn`
- `agent/graph/hooks/framework.py:hooks_at()` — Composes plugin and framework hooks for graph build, with repair before plugins at `before_turn`
- `agent/hooks/_registry.py:make_hook_runner()` — The hook loop; `agent/graph/hooks/node.py:hook_node()` wraps it at graph build time
- `agent/hooks/__init__.py` — Public API re-exports (`Hook`, `HookName`, `make_hook_runner`)
- `agent/hooks/understanding_chunks.py` — when understanding chunks are enqueued, after an llm turn and at compaction: [Understanding Chunk Producers](understanding-chunks.md)
- `agent/hooks/history_dump.py:dump_history()` — the pre-compact JSONL dump of the conversation: [History Dump](history-dump.md)

## Notes

- Workspace cwd validation (`ValidateCwdAfterInitHook`) runs directly inside `init_context_node` before the standing message head is laid down.
- A hook returning `None` means "no modification"—it produces no state update
- Built-in `before_turn` hooks (repair / compact reminder / skill recall / capability-index drift) — the unconditional built-ins and their registration order: [Built-in before_llm Hooks](../../graph/hooks/docs/builtin-before-llm.md).
- Plugin hooks are also `Hook` subclasses (e.g., `meiva_builtins/plugins/meiva_self_evolution/agent_runtime.py`, `meiva_builtins/plugins/meiva_memory/agent_runtime.py`, `meiva_builtins/plugins/meiva_to_user/agent_runtime.py`), written in the same style as built-in hooks—plugin authors don't need to care about built-in vs plugin distinction.

`tests/fakes.py` gives fake cursors `fetchone=None` and `fetchall=[]` to mean no
pending interruption. A truthy default mock can falsely trigger cancellation
or termination probes and their watcher path.
