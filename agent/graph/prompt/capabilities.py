# meiva:note
# The `# Capabilities` section is the system prompt's single index of available skills and live MCP servers, rendering only non-empty halves and omitting the section when both are empty. It directs the agent to match every task against the index before starting so established playbooks and domain capabilities are not skipped or rebuilt. Skill resolution shares one resolver across the index and preloaded skills (`skill_selection.py`), folding unexpanded descendants under their nearest visible ancestor skill on wildcard selection while unfolding explicitly configured subtrees or sub-skills and preserving full catalog membership for drift tracking and preloading. Each skill entry carries its flattened 300-character description and `SKILL.md` file path (`base.packages.skills.names:skill_md_path`, matching `meiva.help`); recall candidates exclude preloaded skills. Disable checks honor the configured entries and the runtime removals folded into `SkillView.disabled`, so a disabled surface renders nothing. Every function here is a function of its `SkillView` and server list; the live reads that fill them are in `live.py`. Index drift diffs live membership in one scan and caps additions at 20 lines while advancing the snapshot over full membership. Prompt assembly records no skill attribution and requests no capability report in final output.
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from base.packages.skills.names import match_key, skill_md_path
from base.packages.skills.tree import Skill, identifier, target

from .sdk_scope import disabled_by_sdk_config
from .skill_selection import (
    SkillView,
    indexed_skills,
    resolve_prompt_skills,
    skill_identifiers,
    unfolded_skill_keys,
)

_DESC_MAX_CHARS = 300


@dataclass(frozen=True)
class McpServer:
    name: str
    description: str | None


class RecallCandidate(NamedTuple):
    identifier: str
    description: str
    target: str
    skill_md: str = ""


def _one_line(description: str) -> str:
    flat = " ".join(description.split())
    return flat if len(flat) <= _DESC_MAX_CHARS else flat[: _DESC_MAX_CHARS - 1] + "…"


def index_entry(path: str, description: str, *, skill_md: str | None = None) -> str:
    loc = f" (`{skill_md}`)" if skill_md else ""
    return f"- `meiva.skills.{path}` — {description}{loc}"


def _index_line(skill: Skill) -> str:
    return index_entry(
        target(skill), _one_line(skill["description"]), skill_md=skill_md_path(skill["path"])
    )


def recall_candidates(view: SkillView) -> list[RecallCandidate]:
    preloaded = set(skill_identifiers(resolve_prompt_skills(view.expand, view).skills))
    candidates: list[RecallCandidate] = []
    for skill in indexed_skills(view):
        ident = identifier(skill)
        if ident in preloaded:
            continue
        candidates.append(
            RecallCandidate(
                ident,
                _one_line(skill["description"]),
                target(skill),
                skill_md_path(skill["path"]),
            )
        )
    return candidates


def _skill_index_lines(view: SkillView) -> list[str]:
    skills = indexed_skills(view)
    if not skills:
        return []
    by_path = {match_key(identifier(s)): s for s in skills}
    unfolded = unfolded_skill_keys(view)
    visible: list[str] = []
    folded_counts: dict[str, int] = {}
    for path in by_path:
        segments = path.split(".")
        ancestors = [".".join(segments[:i]) for i in range(1, len(segments))]
        present_ancestors = [parent for parent in ancestors if parent in by_path]
        if path in unfolded or not present_ancestors:
            visible.append(path)
            folded_counts.setdefault(path, 0)
            continue
        holder = next(
            (parent for parent in reversed(present_ancestors) if parent in folded_counts),
            present_ancestors[0],
        )
        folded_counts[holder] = folded_counts.get(holder, 0) + 1
    lines: list[str] = []
    for path in visible:
        skill = by_path[path]
        line = _index_line(skill)
        count = folded_counts.get(path, 0)
        if count:
            line += (
                f" ({count} sub-skills; inspect with `meiva.help(meiva.skills.{target(skill)})`)"
            )
        lines.append(line)
    return lines


_NEW_SKILLS_FRAMING = (
    "Additional skills are now available beyond those listed in `# Capabilities`. "
    "The descriptions below are summaries; read a skill's `SKILL.md` file at the path shown "
    "before using it (`meiva.help(meiva.skills.<path>)` also inspects an entry and its sub-skills)."
)


NEW_SKILLS_MAX_ENTRIES = 20


def new_skills_note_text(skills: Sequence[Skill]) -> str:
    listed = skills[:NEW_SKILLS_MAX_ENTRIES]
    lines = [_index_line(s) for s in listed]
    if len(skills) > len(listed):
        lines.append(
            f"- … and {len(skills) - len(listed)} more — "
            "`meiva.help(meiva.skills)` enumerates the full catalog"
        )
    return _NEW_SKILLS_FRAMING + "\n\n" + "\n".join(lines)


def _mcp_index_lines(servers: Sequence[McpServer], disabled: Sequence[str]) -> list[str]:
    if disabled_by_sdk_config("mcps", disabled):
        return []
    return [
        f"- `meiva.mcps.{server.name}` — {_one_line(server.description)}"
        if server.description
        else f"- `meiva.mcps.{server.name}`"
        for server in servers
    ]


_WHAT_SKILLS = "reusable skills (playbooks)"
_WHAT_MCPS = "live MCP tool servers"

_SKILLS_HOWTO = (
    "Before using a skill, read its `SKILL.md` file at the path shown on its entry "
    "(the skill's other files live beside it). For folded sub-skills or unlisted skills, "
    "run `meiva.help(meiva.skills.<path>)` to see an entry's description, `SKILL.md` path, "
    "and children. This listing may be a subset of what is installed — "
    "`meiva.help(meiva.skills)` enumerates the full catalog, and an unlisted skill "
    "is still reachable by name. Entry skills group their unexpanded sub-skills: inspect "
    "the matching entry to discover specialized playbooks before choosing one."
)

_MCP_HOWTO = (
    "`meiva.help(meiva.mcps)` lists every configured server and "
    "`meiva.help(meiva.mcps.<server>)` lists one server's tools."
)

_MATCH_EVERY_TASK = (
    "This is your index of what you can already do. Match every task "
    "against it before you start — that is the only thing standing between "
    "you and skipping an established playbook or rebuilding something "
    "(workflow, research, email, browser, transcription, feeds, ...) "
    "that already exists here."
)


def capability_index_is_empty(view: SkillView, servers: Sequence[McpServer]) -> bool:
    return not _skill_index_lines(view) and not _mcp_index_lines(servers, view.disabled)


def capabilities_section(view: SkillView, servers: Sequence[McpServer]) -> str:
    skill_lines = _skill_index_lines(view)
    mcp_lines = _mcp_index_lines(servers, view.disabled)
    if not skill_lines and not mcp_lines:
        return ""

    what = " and ".join(
        w for w, present in ((_WHAT_SKILLS, skill_lines), (_WHAT_MCPS, mcp_lines)) if present
    )
    intro = [
        f"{what[0].upper()}{what[1:]} you already have. "
        "Each line is only a one-line summary — never act on it alone."
    ]
    if skill_lines:
        intro.append(_SKILLS_HOWTO)
    if mcp_lines:
        intro.append(_MCP_HOWTO)

    parts = ["# Capabilities\n\n" + " ".join(intro) + "\n\n" + _MATCH_EVERY_TASK]
    if skill_lines:
        parts.append("**Skills** — reusable playbooks:\n" + "\n".join(skill_lines))
    if mcp_lines:
        parts.append(
            "**MCP servers** — live tools, call `meiva.mcps.<server>.<tool>(...)`:\n"
            + "\n".join(mcp_lines)
        )
    return "\n\n".join(parts)
