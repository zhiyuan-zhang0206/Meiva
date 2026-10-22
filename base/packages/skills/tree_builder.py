from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from base.log import logger
from base.packages.skills.index import SkillFile, SkillIndex
from base.packages.skills.names import SkillIdentity, match_key
from base.packages.skills.tree import NS, Leaf, Skill, Tree, claim, descend, flatten

__all__ = ["SkillIndexBuilder", "scan_tree", "skills_in"]


# meiva:note
# Builds the skill namespace tree from mount roots using `SkillIndex.cached`,
# resetting `_claimed` per root (so a later provider root can override an earlier
# one while same-root collisions raise `SkillNameCollision`) and deduplicating
# identical SKILL.md content across roots via `_seen_hashes`.
# meiva:link meiva/skills/docs/skills.meiva.okf.md
class SkillIndexBuilder:
    def __init__(self, base_ns: tuple[str, ...] = ()) -> None:
        self.tree: Tree = {}
        self._base_ns = base_ns
        self._claimed: dict[tuple[str, ...], str] = {}
        self._seen_hashes: set[str] = set()

    def mount(self, root: Path, gate: set[str] | None) -> None:
        if not root.is_dir():
            return
        self._claimed = {}
        gate_keys = None if gate is None else {match_key(g) for g in gate}
        index = SkillIndex.cached([root])
        for entry in index.entries:
            self._mount_folder_skill(root, entry, gate_keys)
            self._mount_folder_index(entry, gate_keys)

    def _mount_folder_skill(self, root: Path, entry: SkillFile, gate_keys: set[str] | None) -> None:
        skill_md = entry.skill_md
        if skill_md is None:
            return
        rel = entry.rel
        if gate_keys is not None and rel and match_key(rel[0]) not in gate_keys:
            return
        if entry.content_hash is not None:
            if entry.content_hash in self._seen_hashes:
                return
            self._seen_hashes.add(entry.content_hash)
        if entry.error is not None:
            logger.warning("skipping malformed skill {}: {}", skill_md, entry.error)
            return
        if entry.security_rules:
            logger.warning(
                "refusing to mount skill {}: supply-chain scan flagged {}",
                skill_md,
                ", ".join(entry.security_rules),
            )
            return
        name = entry.name
        description = entry.description
        if name is None or description is None:
            raise RuntimeError(f"skill index contract violated for {skill_md}")
        skill: Skill = {
            "name": name,
            "description": description,
            "path": str(entry.folder),
            "namespace": (),
        }
        if rel:
            SkillIdentity.from_dir(rel[-1]).verify(SkillIdentity.from_frontmatter(name))
        ns = self._base_ns + rel[:-1] if rel else self._base_ns
        skill["namespace"] = ns
        parent = descend(self.tree, ns)
        key = match_key(skill["name"])
        self._claim_paths(root, rel, key, str(skill_md.parent))
        existing = parent.get(key)
        if isinstance(existing, NS):
            existing.skill = skill
        else:
            parent[key] = Leaf(skill)
            self._maybe_promote(parent, key, skill, ns)

    def _mount_folder_index(self, entry: SkillFile, gate_keys: set[str] | None) -> None:
        index_md = entry.index_md
        if index_md is None:
            return
        rel = entry.rel
        if not rel or (gate_keys is not None and match_key(rel[0]) not in gate_keys):
            return
        if entry.index_text is None:
            return
        descend(self.tree, self._base_ns + rel).doc = entry.index_text

    def _claim_paths(self, root: Path, rel: tuple[str, ...], key: str, src: str) -> None:
        attr_path = tuple(match_key(seg) for seg in self._base_ns)
        for depth in range(len(rel) - 1):
            attr_path += (match_key(rel[depth]),)
            claim(self._claimed, attr_path, str(root.joinpath(*rel[: depth + 1])))
        claim(self._claimed, (*attr_path, key), src)

    def _maybe_promote(self, parent: NS, key: str, skill: Skill, ns: tuple[str, ...]) -> None:
        if ns and parent.skill is None and key == match_key(ns[-1]):
            parent.skill = {
                "name": skill["name"],
                "description": skill["description"],
                "path": skill["path"],
                "namespace": ns[:-1],
            }


# meiva:link meiva/skills/docs/skills.meiva.okf.md
def scan_tree(skills_root: Path, gate: set[str] | None, provider_roots: Sequence[Path]) -> Tree:
    builder = SkillIndexBuilder()
    builder.mount(skills_root, gate)
    for root in provider_roots:
        builder.mount(root, None)
    return builder.tree


# meiva:link meiva/skills/docs/skills.meiva.okf.md
def skills_in(roots: list[Path]) -> list[Skill]:
    builder = SkillIndexBuilder()
    for root in roots:
        builder.mount(root, None)
    return flatten(builder.tree)
