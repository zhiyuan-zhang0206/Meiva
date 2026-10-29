---
type: doc
title: Hook-module `import meiva` lint
description: Why a module where graph hooks run imports no `meiva`, what counts as a hook module, the known gap, and the entry that supplies the scan directories.
tags:
- scripts
- lint
---

# Hook-module `import meiva` lint

`scripts/lint/plugins/no_meiva_in_hooks.py` (pre-commit `lint-no-meiva-in-hooks`) forbids
`import meiva` in the modules where graph hooks live.

## Why

A graph hook runs in the agent host, a process serving many agents' turns. It operates on
the graph `state` it was handed and returns an update dict for LangGraph's reducer. The
`meiva` SDK is what agent code calls inside its exec child: its state slot (`meiva.state`)
exists only there, and its other state (identity, the exec turn) is per-process. A hook that
imports `meiva` reaches for SDK state that is not that agent's — or not there at all, which
is how the meiva_code after_exec hook read `meiva.state` in the host, got nothing, and silently
injected nothing. Keeping `meiva` out of hook modules makes the mistake impossible to write.

## Rule

A module is a hook module when it is any of:

- under `agent/hooks/`;
- named `agent_runtime.py` (a plugin's host-side face: its hooks and contributions);
- a module that defines a class deriving from `Hook` (`class X(Hook)` / `class X(hooks.Hook)`).

No import of `meiva` or `meiva.<anything>` may appear anywhere in it, including inside a
function or under `TYPE_CHECKING`. State a hook needs comes from its `state` argument
(`PluginStateHandle.view` / `.delta` for plugin state); an SDK-derived value it needs is
computed by a helper in a module that is not a hook module. There is no exemption and no
allowlist. Tests are not scanned.

Known gap: an `importlib.import_module("meiva...")` string or a module a hook module imports
that itself imports `meiva` is not seen — the rule is about the hook module's own imports.
Error format `file:line: <detail>`, non-zero exit.

## Entry

`.venv/bin/python scripts/lint/plugins/no_meiva_in_hooks_cli.py [path ...]` (defaults to the
first-party packages; an explicit path that does not exist is an error). The rule module takes
its scan directories as a value, so the entry is the one place that reads the roles the root
manifest declares. Also run by pre-commit and by the CI structure job.

Parent: [[scripts/lint/docs/lint.meiva.okf.md|lint]].
