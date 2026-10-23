"""Implementation of the `meiva` SDK entry surface — what the agent sees and how —
plus the per-call plumbing every SDK namespace shares.

Each module holds one cohesive slice of the entry-point machinery:

- `const` — the `meiva.const()` documented-value factory;
- `sdk_disable` — the `MEIVA_SDK_DISABLE` machinery (parse + sentinel swap);
- `discovery` — kind predicates, `_Constant`, and the module/class child
  walkers behind `agent_visible_names` (the single source of truth shared by
  help rendering, SDK-expand discovery, doc linting, and metering);
- `help` — the `meiva.help()` renderer (stub-format docs for SDK targets);
- `installation` — the frozen `Installation` value (wrap layers, skill
  providers, metering ledger, applied-disable set, faces flag), the single slot
  on the `meiva` module that holds it, and `installed()`, the read every other SDK
  piece shares;
- `install` — the one writer of the `meiva` module: installs a registry's SDK
  surface (namespaces, members, expansions, wraps, skill sources, flags, config)
  and undoes it, producing one `Installation` — a change builds a new value and
  swaps the holder;
- `process_context` — the process-local SDK entry: the context and exec-turn state slots behind
  `meiva.context` / `meiva.state` / `meiva.state_update`, the shared-host posture and the loaded code
  image (`meiva/boot.py` builds the contexts and clients and loads plugins above it);
- `plugins` — the namespace / member install primitives and their exception
  hierarchy;
- `wraps` — the wrap layer primitive `install` applies
  (`meiva/__init__.py` builds the curated `meiva.extend` surface from it);
- `extensions` — `load_extensions`: asks `base.packages.plugins.loading` (plugin discovery, the
  by-path loader, the registry of what plugins declare) to import the enabled plugins' faces and
  hands the result to `install`;
- `skill_sources` — plugin-contributed skill-root providers, carried by the
  installation and kept off the `meiva.skills` namespace so the installer can write
  them even when `MEIVA_SDK_DISABLE` stubs that namespace out; `scoped` holds one
  extra for the duration of a call.

The per-call plumbing the namespaces (`meiva.files`, `meiva.web`, ...) share:

- `validation` — argument coercion (`coerce_str` / `coerce_typed`) at every SDK
  entry point, plugin namespaces included;
- `batch` — the bounded concurrent executor behind the SDK batch APIs
  (`meiva.web.search` / `meiva.web.fetch` / `meiva.understand`);
- `metering` — the transparent per-call recorder that emits one `sdk_call`
  event per top-level `meiva.*` call.

`meiva/__init__.py` re-exports the framework entry points (`meiva.help`, ...). The
agent kernel drives rendering by passing `help()` its render parameters, and reads
the installed surface through `installation.installed()` / `install.expansions()` and the
`sdk_disable` entries. None of it is agent-facing: `help(meiva)` lists only the
`__all_for_meiva__` whitelist, and `MEIVA_SDK_DISABLE` refuses to disable a
framework module such as this package.
"""

import sys as _sys
from enum import StrEnum
from types import ModuleType
from typing import Any


def meiva_module() -> ModuleType:
    """The `meiva` package module itself — reached through `sys.modules` so
    these private modules never create an import cycle during `import meiva`."""
    return _sys.modules["meiva"]


class SdkProcessPurpose(StrEnum):
    SHARED_HOST = "shared_host"


class ContextOutsideProcessError(AttributeError):
    pass


class PluginStateOutsideTurnError(AttributeError):
    pass


CONTEXT_KEY = "_exec_context"
STATE_KEY = "_exec_state"
UPDATE_KEY = "_exec_state_update"
IMAGE_KEY = "_LOADED_IMAGE"


def _slots() -> dict[str, Any]:
    return meiva_module().__dict__


def outside_exec_turn(name: str) -> PluginStateOutsideTurnError:
    return PluginStateOutsideTurnError(
        f"meiva.{name} exists only inside execute_code (an exec turn); this process has no bound "
        "turn state. Host code reads the graph state its hook was handed instead."
    )


def outside_process(name: str) -> ContextOutsideProcessError:
    return ContextOutsideProcessError(
        f"meiva.{name} requires an execution child, launched script or external attachment; "
        "host code uses the context its caller supplied"
    )


def get_state() -> object:
    value = _slots().get(STATE_KEY)
    if value is None:
        raise outside_exec_turn("state")
    return value


def get_state_update() -> object:
    slots = _slots()
    if slots.get(STATE_KEY) is None:
        raise outside_exec_turn("state_update")
    return slots.get(UPDATE_KEY)


def in_exec_turn() -> bool:
    return _slots().get(STATE_KEY) is not None
