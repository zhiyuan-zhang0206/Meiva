"""The materialized skill index — doorplate ⑤ (R3).

One scan behind every skill read path. Before this module each consumer walked
the skill tree itself: the runtime loader (meiva.skills), the gateway panel, the
repo frontmatter lint — four traversals, no cache, and the lint's separate
traversal drifted once (2026-08: a three-deep skill shipped with malformed
frontmatter because the lint globbed only two levels). This module is the
single scan: `SkillIndex.build()` is the pure builder, `SkillIndex.cached()`
wraps it in an mtime-keyed process cache, so repeated scans — the system prompt
is rebuilt before every LLM call — cost a directory stat walk, not file reads.

The index is an *inventory of skill-bearing folders*, not a namespace tree:
one entry per folder under a root that carries a SKILL.md and/or an INDEX.md,
with the parsed frontmatter (name/description), a content hash for cross-root
dedup, and file stats for cache invalidation. Tree building, namespace
mounting, gating and collision refusal stay in `meiva.skills` (R2's identity
entity); this module never builds a filesystem path out of a name — the key
fold is `base.packages.skills.names.match_key`, the same comparison key every other
skill surface uses.

Tolerance contract: `build()` never raises on a bad skill. A malformed or
unreadable SKILL.md becomes an entry with `error` set; consumers decide — the
runtime loader skips with a warning, the merge lint fails. One parser: the
frontmatter checks here (structural parse + required name/description fields)
are exactly what `meiva.skills._parse_frontmatter` enforced before this module
existed.

Supply-chain gate: every SKILL.md/INDEX.md body is ALSO run through the
`base.packages.skills.scan` critical rule table (the same rules the CLI install path
gates on). A critical hit lands on the entry as `security_rules` — the runtime
loader refuses to mount it, closing the project-skill bypass (a `git clone`
drops `.claude/skills/` into the scan with no user action).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from base.packages.skills.index_plan import (
    INDEX_MD,
    SKILL_MD,
    FileKind,
    SkillFile,
    assemble_skill_file,
    file_unchanged,
    folder_snapshot,
    key_index,
    skill_md_facts,
)
from base.packages.skills.names import match_key


def _read_utf8(path: Path) -> tuple[str | None, OSError | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, exc


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _regular_file_stat(
    path: Path, stat_of: Callable[[Path], os.stat_result | None]
) -> os.stat_result | None:
    try:
        st = stat_of(path)
    except OSError:
        return None
    return st if FileKind.of(st) is FileKind.REGULAR else None


def _folders_in(
    root: Path, stat_of: Callable[[Path], os.stat_result | None]
) -> list[tuple[Path, int]] | None:
    folders: list[tuple[Path, int]] = []
    for p in sorted({root, *root.rglob("*")}):
        try:
            st = stat_of(p)
        except OSError:
            continue
        if st is not None and FileKind.of(st) is FileKind.DIRECTORY:
            folders.append((p, st.st_mtime_ns))
    return folders or None


def _skill_file(
    folder: Path, root: Path, stat_of: Callable[[Path], os.stat_result | None]
) -> SkillFile | None:
    """Parse one folder's SKILL.md / INDEX.md; None when it holds neither."""
    skill_md = folder / SKILL_MD
    index_md = folder / INDEX_MD
    skill_stat = _regular_file_stat(skill_md, stat_of)
    index_stat = _regular_file_stat(index_md, stat_of)
    skill_facts = skill_md_facts(*_read_utf8(skill_md)) if skill_stat is not None else None
    index_text = _read_utf8(index_md)[0] if index_stat is not None else None
    return assemble_skill_file(
        folder,
        root,
        skill_stat=skill_stat,
        index_stat=index_stat,
        skill_facts=skill_facts,
        index_text=index_text,
    )


def _scan_root(
    root: Path, stat_of: Callable[[Path], os.stat_result | None] = _stat_or_none
) -> tuple[tuple[SkillFile, ...], tuple[tuple[tuple[str, ...], int], ...]]:
    """Walk one root: parse every SKILL.md/INDEX.md, stat every folder.

    Returns (entries, folder_stats) — folder_stats covers ALL folders (not
    just skill-bearing ones) so `SkillIndex._fresh` can detect a new skill
    folder appearing anywhere under the root.
    """
    folders = _folders_in(root, stat_of)
    if folders is None:
        return (), ()
    entries = [
        entry for folder, _ in folders if (entry := _skill_file(folder, root, stat_of)) is not None
    ]
    return tuple(entries), folder_snapshot(root, folders)


class SkillIndex:
    """A scanned skill tree: every skill-bearing folder under a set of roots.

    Immutable once built; `cached()` reuses a build while the underlying tree
    is unchanged (folder set + every file stat identical).
    """

    __slots__ = ("_by_key", "entries", "root_folders", "roots")

    def __init__(
        self,
        roots: tuple[Path, ...],
        root_folders: tuple[tuple[tuple[tuple[str, ...], int], ...], ...],
        entries: tuple[SkillFile, ...],
    ) -> None:
        self.roots = roots
        self.root_folders = root_folders
        self.entries = entries
        self._by_key = key_index(entries)

    @classmethod
    def build(
        cls,
        roots: list[Path] | tuple[Path, ...],
        *,
        stat_of: Callable[[Path], os.stat_result | None] = _stat_or_none,
    ) -> SkillIndex:
        """Pure builder: walk every root, read and parse every SKILL.md /
        INDEX.md. Tolerant (errors land on entries); never raises on a bad
        skill."""
        roots = tuple(roots)
        entries: list[SkillFile] = []
        root_folders: list[tuple[tuple[tuple[str, ...], int], ...]] = []
        for root in roots:
            root_entries, folders = _scan_root(root, stat_of)
            entries.extend(root_entries)
            root_folders.append(folders)
        return cls(roots, tuple(root_folders), tuple(entries))

    @classmethod
    def cached(cls, roots: list[Path] | tuple[Path, ...]) -> SkillIndex:
        """`build()` behind an mtime-keyed process cache: returns the previous
        build for the same roots while a re-stat shows the tree unchanged,
        rebuilds (and re-reads) only when something changed. Clearing the cache
        is `clear_cache()` (tests / a plugin reload)."""
        roots = tuple(roots)
        prev = _CACHE.get(roots)
        if prev is not None and prev._fresh():
            return prev
        idx = cls.build(roots)
        _CACHE[roots] = idx
        return idx

    @classmethod
    def clear_cache(cls) -> None:
        """Drop every cached index. Tests and the kernel's plugin reload use
        this; normal operation never needs it (mtime invalidation suffices)."""
        _CACHE.clear()

    def _fresh(self) -> bool:
        """True when a re-stat of the tree still matches this index: the same
        folders with the same mtimes, and every indexed file with the same
        mtime and size. A new SKILL.md anywhere (folder mtime), an edit (file
        mtime/size), or a delete (folder set) each read as stale."""
        for root, saved in zip(self.roots, self.root_folders, strict=True):
            folders = _folders_in(root, _stat_or_none)
            if folders is None or folder_snapshot(root, folders) != saved:
                return False
        for e in self.entries:
            for path, mtime, size in (
                (e.skill_md, e.skill_mtime_ns, e.skill_size),
                (e.index_md, e.index_mtime_ns, e.index_size),
            ):
                if path is not None and not file_unchanged(
                    _stat_or_none(path), mtime_ns=mtime, size=size
                ):
                    return False
        return True

    def lookup(self, name: str) -> SkillFile | None:
        """The entry whose frontmatter name folds to `match_key(name)`, or
        None. First entry wins on a collision (the scan order is roots then
        sorted folders, the same order the runtime mount converges under)."""
        return self._by_key.get(match_key(name))


_CACHE: dict[tuple[Path, ...], SkillIndex] = {}
