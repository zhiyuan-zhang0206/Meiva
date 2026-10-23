# meiva:note
# Which skills a prompt surface covers, decided from values. `SkillView` carries the two
# configured lists, the effective disable entries and the live skill catalog; `live.py` reads
# it once from the running process and every decision here is a function of it. Configured
# names match a skill's identifier first and its bare name second, spelling-insensitively; a
# `*` selects the whole catalog. Names that match nothing are returned, not logged, so the
# caller that read the view reports them.
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from base.packages.skills.names import match_key
from base.packages.skills.tree import Skill, identifier

from .sdk_scope import disabled_by_sdk_config


@dataclass(frozen=True)
class SkillView:
    inject: tuple[str, ...]
    expand: tuple[str, ...]
    disabled: tuple[str, ...]
    skills: tuple[Skill, ...]


class Selection(NamedTuple):
    skills: tuple[Skill, ...]
    unresolved: tuple[str, ...]


class IndexDrift(NamedTuple):
    identifiers: set[str]
    added: list[Skill]


def resolve_prompt_skills(wanted: Sequence[str], view: SkillView) -> Selection:
    if disabled_by_sdk_config("skills", view.disabled) or not wanted:
        return Selection((), ())
    if "*" in wanted:
        return Selection(view.skills, ())
    by_ident = {match_key(identifier(s)): s for s in view.skills}
    by_name = {match_key(s["name"]): s for s in view.skills}
    selected: list[Skill] = []
    unresolved: list[str] = []
    for name in wanted:
        key = match_key(name)
        skill = by_ident.get(key) or by_name.get(key)
        if skill is None:
            unresolved.append(name)
        else:
            selected.append(skill)
    return Selection(tuple(selected), tuple(unresolved))


def indexed_skills(view: SkillView) -> list[Skill]:
    return list(resolve_prompt_skills(view.inject, view).skills)


def skill_identifiers(skills: Sequence[Skill]) -> list[str]:
    return [identifier(s) for s in skills]


def indexed_skill_identifiers(view: SkillView) -> set[str]:
    return set(skill_identifiers(indexed_skills(view)))


def index_drift(known: set[str], view: SkillView) -> IndexDrift:
    live = indexed_skills(view)
    return IndexDrift(
        identifiers=set(skill_identifiers(live)),
        added=[s for s in live if identifier(s) not in known],
    )
