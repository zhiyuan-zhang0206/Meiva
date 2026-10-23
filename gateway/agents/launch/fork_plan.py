# meiva:note
# The fork rule: what a fork's config overlay may differ in from its source.
#
# A fork keeps the source agent's effective config so the inherited context cache stays valid.
# The only change allowed is ADDING skills to the two skill lists, which are loaded at the
# context tail. The shell reads the source's overlay and birth config; this module decides what
# overlay the fork stores and which skills it adds, or why the request is refused.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from base.api_contracts.agent_contract import ForkConfigChangeNotAllowed

__all__ = ["FORK_SKILL_KEYS", "ForkConfigPlan", "fork_config_plan"]

FORK_SKILL_KEYS = ("skills_to_inject_into_system_prompt", "skills_to_expand_at_start")


# meiva:note
# The overlay the fork stores and the skills it adds at the context tail.
@dataclass(frozen=True)
class ForkConfigPlan:
    config: dict[str, object] | None
    tail_skills: list[str] | None


# meiva:note
# A skill list as typed as it can be: None for anything that is not a list of strings
# (a missing key included), since the fork rule only reasons about real skill lists.
def _skill_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    strings = [item for item in items if isinstance(item, str)]
    return strings if len(strings) == len(items) else None


# meiva:note
# Overlay keys the fork may not change: any non-skill-list difference or a skill reduction.
def _offending_keys(
    merged: Mapping[str, object],
    source_overlay: Mapping[str, object],
    source_effective: Mapping[str, object],
    fork_effective: Mapping[str, object],
) -> list[str]:
    offending: list[str] = []
    for key in set(merged) | set(source_overlay):
        if fork_effective.get(key) == source_effective.get(key):
            continue
        if key not in FORK_SKILL_KEYS:
            offending.append(key)
            continue
        fork_list = _skill_list(fork_effective.get(key))
        source_list = _skill_list(source_effective.get(key))
        if fork_list is None or source_list is None or not set(source_list) <= set(fork_list):
            offending.append(key)
    return offending


# meiva:note
# The fork's overlay and tail skills, or the refusal for a config change the rule forbids.
def fork_config_plan(
    source_overlay: Mapping[str, object],
    source_birth: Mapping[str, object],
    requested: Mapping[str, object] | None,
) -> ForkConfigPlan | ForkConfigChangeNotAllowed:
    if requested is None:
        # meiva:note
        # No change requested: inherit the source's overlay verbatim so the fork's effective
        # config equals the source's (dropping it would silently re-brain the fork).
        return ForkConfigPlan(dict(source_overlay) if source_overlay else None, None)

    # meiva:note
    # The fork's stored overlay is the SOURCE's overlay plus the fork's own fields; the diff
    # runs on that merged map (fields the fork names that repeat the source's value are a
    # no-op, not a change).
    merged = {**source_overlay, **requested}
    source_effective = {**source_birth, **source_overlay}
    fork_effective = {**source_birth, **merged}
    offending = _offending_keys(merged, source_overlay, source_effective, fork_effective)
    if offending:
        return ForkConfigChangeNotAllowed(
            f"fork may not change config overlay keys {sorted(offending)!r} — a fork "
            f"keeps the source agent's config so its context cache stays valid; only "
            f"ADDING skills to {FORK_SKILL_KEYS!r} is allowed (loaded at the context "
            "tail). Change the fork after it exists via restart(config_overlay=...) "
            "instead."
        )
    inject_key, expand_key = FORK_SKILL_KEYS
    source_inject = set(_skill_list(source_effective.get(inject_key)) or [])
    fork_inject = set(_skill_list(fork_effective.get(inject_key)) or [])
    fork_expand = set(_skill_list(fork_effective.get(expand_key)) or [])
    delta = sorted(skill for skill in fork_inject - source_inject if skill not in fork_expand)
    return ForkConfigPlan(merged, delta or None)
