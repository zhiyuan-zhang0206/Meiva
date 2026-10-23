---
type: doc
title: Agent Hooks
description: Meiva plugin's graph-edge hook system—four hook container nodes (after_init / before_llm / before_exec / after_exec) provide extension points in the LangGraph execution graph. Plugins register instances of Hook subclasses; the framework calls their typed __call__ in registration order during graph node execution, with reducer arbitration for co-writes.
tags: []
---

# Agent Hooks

## What it is

Meiva plugin's **graph-edge hook system**—four hook container nodes (`after_init / before_llm / before_exec / after_exec`) provide extension points in the LangGraph execution graph. Plugins register instances of `Hook` subclasses; the framework calls their typed `__call__` in registration order during graph node execution, with reducer arbitration for co-writes.

Together with SDK wraps (a plugin's declared `SdkWrap`, applied by `meiva/sdk_surface/install.py` through `meiva/sdk_surface/wraps.py`), this forms the two-layer plugin system: SDK wraps modify the behavior of the meiva namespace, while graph-edge hooks intervene in the agent execution pipeline.

## Core Responsibilities

- **`Hook` base class** (`agent/hooks/_registry.py:Hook`, `ABC`): subclasses override the typed `async def __call__(self, state, runtime, config, /) -> dict | None`. The base locks the signature—under pyright strict, incompatible overrides raise `reportIncompatibleMethodOverride`; not overriding leaves an uninstantiable abstract class. Optional `.name` property (defaults to class name) labels co-write conflict messages.
- **Hook declaration**: a plugin's `contribute()` returns `PluginContributions(before_llm=(...), before_exec=(...), after_exec=(...), after_init=(...))` of `Hook` **instances** (not class, not bare function); nothing is registered at import. The framework's own are `agent/graph/hooks/framework.py:framework_hooks()`
- **Hook execution**: `agent/graph/hooks/framework.py:hooks_at` composes one `(plugin | None, hook)` sequence per edge (repair first at `before_llm`, then plugins, then the remaining framework hooks); every hook receives the same original `(state, runtime, config)` and only returned deltas merge in execution order — no priority system, no intermediate state mutation. Ordering rationale: [[agent/hooks/docs/hook-execution.meiva.okf.md|Hook Execution]]
- **Hook points**: `HookName = HookPoint = Literal["before_llm", "before_exec", "after_exec", "after_init"]` (`base/packages/plugins/extensions.py`)
- **Co-write arbitration**: two hooks writing the same state key are merged by that key's reducer; with no reducer the runner raises `RuntimeError` fail-loud instead of a silent last-wins ([[agent/hooks/docs/hook-execution.meiva.okf.md|Hook Execution]])
- **Instance state**: subclasses carry per-hook state/configuration in `__init__` on `self`—declarations hold instances, not bare functions. Built-in `_CompactReminderHook` / `_RepairDanglingToolPairingHook` are module-level singletons that `framework_hooks()` returns.
- **Route override**: If the dict returned by a hook contains `"goto": NodeName`, the container node's default route is overridden; other keys are treated as part of `Command(update=...)`
- **Activation telemetry**: a hook declared by a *plugin* that returns a non-empty dict also emits one `plugin_activation` event naming the keys it wrote (`base/packages/plugins/activation.py`; attribution is the plugin name paired with the hook in the runner's `hooks`). Framework hooks and `None` returns record nothing, so pure observation stays free. This is the runtime half of the registration ledger and philosophy §6's obsolescence gauge — see [[docs/plugins/plugins.meiva.okf.md]]

## Key Dependencies

- [[agent/docs/state.meiva.okf.md]] — The hook's `state` parameter is the `AgentState` passed by LangGraph; the returned dict goes through standard LangGraph reducer merging; hooks run at the graph-node level and directly receive/return the whole state. A plugin hook reads its own fields with the handle's pure `view(state)` and returns writes through `delta({...})`; `read` / `update` are the exec child's channel (`meiva.state` / `meiva.state_update` exist only there). A hook module imports no `meiva` (`scripts/lint/plugins/no_meiva_in_hooks.py`)
- [[agent/graph/docs/graph.meiva.okf.md]] — Placement of hook container nodes in the graph topology

## Entry Points

- `agent/hooks/_registry.py:Hook` — Base class; subclasses override `__call__`
- `agent/graph/hooks/framework.py:framework_hooks()` — The framework's own hooks per edge
- `agent/graph/hooks/framework.py:hooks_at()` — Composes plugin and framework hooks for graph build, with repair before plugins at `before_llm`
- `agent/hooks/_registry.py:make_hook_runner()` — The hook loop; `agent/graph/hooks/node.py:hook_node()` wraps it at graph build time
- `agent/hooks/__init__.py` — Public API re-exports (`Hook`, `HookName`, `make_hook_runner`)
- `agent/hooks/understanding_chunks.py` — when understanding chunks are enqueued, after an llm turn and at compaction: [[agent/hooks/docs/understanding-chunks.meiva.okf.md|Understanding Chunk Producers]]
- `agent/hooks/history_dump.py:dump_history()` — the pre-compact JSONL dump of the conversation: [[agent/hooks/docs/history-dump.meiva.okf.md|History Dump]]

## Notes

- **after_init** is the earliest hook point: its container node sits right after `START` and before `init_context`, so its state edits land before the standing message head is laid down (see [[agent/graph/docs/graph.meiva.okf.md]] for placement). Example: the meiva_code plugin registers `validate_cwd_after_init` here to repair a persisted logical cwd that cannot be statted or is not a directory, without mutating the Python process cwd.
- A hook returning `None` means "no modification"—it produces no state update
- Hook signatures are uniform, not distinguishing between nodes—the same `Hook` subclass instance can theoretically be registered at multiple hook points
- Built-in before_llm hooks (compact / repair / capability-index drift) — the three unconditional built-ins and their registration order: [[agent/graph/hooks/docs/builtin-before-llm.meiva.okf.md]].
- Plugin hooks are also `Hook` subclasses (e.g., `meiva_builtins/plugins/meiva_sdk_reminder/agent_runtime.py:_SdkReminderAfterExecHook`, `meiva_builtins/plugins/meiva_code/agent_runtime.py:_InjectCwdNotesAfterExecHook`, `plugins/meiva_memory`'s recall hook, etc.), written in the same style as built-in hooks—plugin authors don't need to care about built-in vs plugin distinction
- The permission hook example (`demos/permission-hooks/sensitive_op_gate.py:_SensitiveOpGateHook`) demonstrates the hook system's access control application: two-stage interception (block/warn), intercepting sensitive operations (force push, delete files, external sends, etc.) in before_exec; when blocking, returns `goto: "after_exec"` to skip the exec node

`tests/fakes.py` gives fake cursors `fetchone=None` and `fetchall=[]` to mean no
pending interruption. A truthy default mock can falsely trigger cancellation
or termination probes and their watcher path.
