# meiva:note
# Which SDK paths are in scope for the system prompt, decided from values. `disabled` is the
# configured `MEIVA_SDK_DISABLE` entries followed by the entries applied to the installation at
# run time; the readers in `live.py` build it, and nothing here looks at the running process.
# `expand_plan` merges the plugin-declared expansions ahead of the configured list, replaces the
# `*` entry with the discovered namespaces, drops repeats and refuses members of a capability
# surface (they would inline a whole skill body or a server's tool schemas into every prompt).
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

CAPABILITY_SURFACES = ("skills", "mcps")


@dataclass(frozen=True)
class ExpandPlan:
    paths: tuple[str, ...]
    refused: tuple[str, ...]


# meiva:note
# Why a configured expand entry never reached the SDK reference. The kind is what travels, not a
# sentence: `note_text.sdk_expand_skipped_text` owns the words the agent reads. `UNRESOLVED` is the
# one the agent is not told about: a first segment that is not in this process is either a typo or a
# plugin this process does not serve, and nothing at run time tells the two apart. The other two are
# provable from this process's own surface, so the note names them.
class ExpandSkip(StrEnum):
    UNRESOLVED = "unresolved"
    MISSING_MEMBER = "missing-member"
    NOT_A_TARGET = "not-a-target"


@dataclass(frozen=True)
class SkippedExpand:
    path: str
    kind: ExpandSkip


def disabled_by_sdk_config(path: str, disabled: Sequence[str]) -> bool:
    return any(path == entry or path.startswith(entry + ".") for entry in disabled)


def is_capability_surface_member(path: str) -> bool:
    head, _, rest = path.partition(".")
    return bool(rest) and head in CAPABILITY_SURFACES


def expand_plan(
    configured: Sequence[str],
    *,
    plugin_expansions: Sequence[str],
    discovered: Sequence[str],
) -> ExpandPlan:
    configured_paths: list[str] = []
    for entry in configured:
        if entry == "*":
            configured_paths.extend(discovered)
        else:
            configured_paths.append(entry)
    seen: set[str] = set()
    resolved: list[str] = []
    refused: list[str] = []
    for path in (*plugin_expansions, *configured_paths):
        if path in seen:
            continue
        seen.add(path)
        if is_capability_surface_member(path):
            refused.append(path)
            continue
        resolved.append(path)
    return ExpandPlan(tuple(resolved), tuple(refused))
