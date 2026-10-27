# meiva:note
# The shell that scans skill roots and mounts what they hold: it takes the entries of each root from
# the mtime-cached `SkillIndex` and hands them to `SkillIndexBuilder`, which decides what is
# mounted, in what namespace and which collisions are refused.

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from base.packages.skills.index import SkillIndex
from base.packages.skills.tree import Skill, Tree, flatten
from base.packages.skills.tree_builder import SkillIndexBuilder


# meiva:link meiva/skills/docs/skills.meiva.okf.md
def scan_tree(skills_root: Path, gate: set[str] | None, provider_roots: Sequence[Path]) -> Tree:
    builder = SkillIndexBuilder()
    builder.mount(skills_root, gate, SkillIndex.cached([skills_root]).entries)
    for root in provider_roots:
        builder.mount(root, None, SkillIndex.cached([root]).entries)
    return builder.tree


# meiva:link meiva/skills/docs/skills.meiva.okf.md
def skills_in(roots: list[Path]) -> list[Skill]:
    builder = SkillIndexBuilder()
    for root in roots:
        builder.mount(root, None, SkillIndex.cached([root]).entries)
    return flatten(builder.tree)
