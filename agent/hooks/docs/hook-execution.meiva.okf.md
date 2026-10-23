---
type: doc
title: Hook Execution — Ordering and Co-Write Arbitration
description: How the graph composes plugin and framework hooks into one sequence per edge (repair first at before_llm), why every hook sees the same original state, and how two hooks that write the same state key are merged by the reducer or rejected fail-loud.
tags: []
---

# Hook Execution — Ordering and Co-Write Arbitration

## Responsibility

The hook container nodes run a fixed sequence of `Hook` instances at each edge
and merge what they return. This node records the sequence and the merge rule;
the `Hook` base class, declaration and hook points are
[[agent/hooks/docs/hooks.meiva.okf.md|Agent Hooks]]'s to state.

## Execution order

- **Hook execution**: `agent/graph/hooks/framework.py:hooks_at` composes the `(plugin | None, hook)` sequence. At `before_llm`, repair runs first, then plugins in their existing order, then the remaining framework hooks; at other edges, plugins precede framework hooks. `make_hook_runner(name, default_next, hooks)` fixes that sequence for the graph's lifetime; `agent/graph/hooks/node.py:hook_node` wraps it in lifecycle events. Every hook receives the same original `(state, runtime, config)`, and only returned deltas merge in execution order. Repair therefore cannot wipe an earlier plugin note while retaining its state update. There is no priority system or intermediate state mutation.

## Co-write arbitration

- **Co-write arbitration**: If two hooks within the same run write to the same state key—if that key has a non-trivial reducer in the state schema (e.g. the `messages` channel's guarded reducer), the runner merges both values using the reducer; if no reducer, raises `RuntimeError` fail-loud (refusing silent last-wins, preventing one hook's wholesale replacement from swallowing another hook's appended content)

## Entry Points

- `agent/graph/hooks/framework.py:hooks_at()` — composes plugin and framework hooks for graph build
- `agent/hooks/_registry.py:make_hook_runner()` — the hook loop; `agent/graph/hooks/node.py:hook_node()` wraps it at graph build time

Parent: [[agent/hooks/docs/hooks.meiva.okf.md|Agent Hooks]].
