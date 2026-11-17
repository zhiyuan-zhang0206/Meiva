# meiva:note
# Which skills a prompt surface covers, decided from values. `SkillView` carries the two
# configured lists, the effective disable entries and the live skill catalog; `live.py` reads
# it once from the running process and every decision here is a function of it. Configured
# names match a skill's identifier first, a `<prefix>.*` namespace subtree second, and its bare
# name third, spelling-insensitively; `*` selects the whole catalog while explicit entries beside
# `*` mark sub-skills or subtrees to keep unfolded in `# Capabilities`. Names that match nothing
# are returned, not logged, so the caller that read the view reports them.
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


def _match_one(
    entry: str,
    skills: Sequence[Skill],
    by_ident: dict[str, Skill],
    by_name: dict[str, Skill],
) -> list[Skill]:
    if entry.endswith((".*", ":*")):
        prefix = match_key(entry[:-2])
        if not prefix:
            return []
        return [
            s
            for s in skills
            if (key := match_key(identifier(s))) == prefix or key.startswith(prefix + ".")
        ]
    key = match_key(entry)
    hit = by_ident.get(key) or by_name.get(key)
    return [hit] if hit is not None else []


def _match_entries(entries: Sequence[str], skills: Sequence[Skill]) -> Selection:
    by_ident = {match_key(identifier(s)): s for s in skills}
    by_name = {match_key(s["name"]): s for s in skills}
    selected: list[Skill] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    for name in entries:
        hits = _match_one(name, skills, by_ident, by_name)
        if not hits:
            unresolved.append(name)
            continue
        for skill in hits:
            ident_key = match_key(identifier(skill))
            if ident_key not in seen:
                seen.add(ident_key)
                selected.append(skill)
    return Selection(tuple(selected), tuple(unresolved))


def resolve_prompt_skills(wanted: Sequence[str], view: SkillView) -> Selection:
    if disabled_by_sdk_config("skills", view.disabled) or not wanted:
        return Selection((), ())
    if "*" in wanted:
        return Selection(view.skills, ())
    return _match_entries(wanted, view.skills)


def unfolded_skill_keys(view: SkillView) -> set[str]:
    skills = indexed_skills(view)
    if "*" not in view.inject:
        return {match_key(identifier(s)) for s in skills}
    explicit = [entry for entry in view.inject if entry != "*"]
    if not explicit:
        return set()
    return {match_key(identifier(s)) for s in _match_entries(explicit, skills).skills}


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
