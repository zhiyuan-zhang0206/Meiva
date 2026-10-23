from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agent.graph.prompt.skill_selection import SkillView
from base.packages.skills import skills_in


def write_skill(root: Path, dirname: str, name: str, description: str, body: str = "") -> None:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}", encoding="utf-8"
    )


def skill_view(
    root: Path,
    *,
    inject: Sequence[str] = ("*",),
    expand: Sequence[str] = (),
    disabled: Sequence[str] = (),
) -> SkillView:
    return SkillView(tuple(inject), tuple(expand), tuple(disabled), tuple(skills_in([root])))
