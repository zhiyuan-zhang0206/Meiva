---
type: doc
title: Memory Pool Scaffold and Validator
description: How `meiva memory init` brings up a cluster's memory pool checkouts and lays down the plugin's template (MEMORY.md index and the pre-commit character-cap guard), and what the pool validator `validate.py` enforces on every commit.
tags:
- plugin
- memory
- extensions
---

# Memory Pool Scaffold and Validator

## Responsibility

The pool is a git checkout the plugin does not write to at runtime. This node
covers the host-side step that creates it and the validator that guards every
commit into it. The plugin as a whole — hooks, SDK surface, configuration — is
[[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md|meiva_memory]]'s to state.

## Host-side scaffolding

**Host-side scaffolding** (`meiva_builtins/plugins/meiva_memory/setup.py:scaffold(*, machine, facts)`, run by explicit `meiva memory init` for every enabled plugin): brings up this cluster's memory pool checkouts and lays the plugin's `template/` down inside the authoring one — `MEMORY.md` (the index every agent reads) and `.githooks/pre-commit` (the character-cap guard), then arms `core.hooksPath`. Idempotent and non-destructive; never overwrites existing pool files. Kept out of `plugin.py`, which imports the agent runtime absent from the `meiva` CLI process. Converge and service startup never touch the memory repository.

## Pool validator

**`python3 validate.py`** (in the pool checkout, run by the pre-commit hook): the pool's rules in one place — frontmatter, exactly one `type/<x>` tag, a non-empty `description`, the index's pointer format (`- [Title](path.md) — description`) and targets, orphaned notes, the character caps, and the directory-structure limits. Exits non-zero on any finding and runs on every commit via `.githooks/pre-commit`.

## Entry Points

- `meiva_builtins/plugins/meiva_memory/setup.py` — `scaffold`, the host-side pool bring-up
- `meiva_builtins/plugins/meiva_memory/template/validate.py` — the pool validator (`validate_bundle` checks a whole pool directory and `main` prints its findings; `validate_note` is the single-note check the write check reuses, see [[meiva_builtins/plugins/meiva_memory/docs/memory-write-check.meiva.okf.md]])

Parent: [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md|meiva_memory]].


## Provisioning targets and configuration stages

The public plugin ABI is `scaffold(*, machine, facts)`: the explicit `meiva memory init` command reads the
Meiva home, the two memory provisioning settings (`MEIVA_MEMORY_KEEP_LOCAL`, `MEIVA_MEMORY_REMOTE`) and the
gateway access once from its configuration owner (`base.packages.plugins.loading.scaffold.ScaffoldFacts`) and hands
that frozen value to every enabled plugin's scaffold; a scaffold written for `scaffold(*, machine)` is refused
before it runs. The scaffold reads no settings of its own and has no root. It derives the authoring and
consolidated paths from `facts.home` through the foundation path helpers, and every later initialization,
template and hook operation uses those targets; the next invocation of the command reads the facts again.

The outer checkout probe retains its logging and Git error-classification
boundary. The actual initialization stage probes that same target again; an
intervening filesystem change is not hidden behind a cached boolean. This is not
an initialization mutex or a concurrency transaction.

Existing and local-only checkouts do not need a remote or gateway access. A fresh remote operation uses
`facts.memory_remote` only when needed; a split runner without it then installs a bundle from
`facts.gateway`. Both stages (authoring and, on a gateway-capable unit, the consolidated checkout) provision
from the same facts, so a setting changed while the first stage runs does not reach the second.
