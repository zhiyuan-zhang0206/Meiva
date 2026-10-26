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

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.packages.docs.frontmatter import FrontmatterError, parse_frontmatter
from base.packages.skills.names import match_key

_REQUIRED_FIELDS = ("name", "description")


class SkillFormatError(ValueError):
    """SKILL.md failed to parse — missing frontmatter, missing required
    `name` or `description` field, or a field is not in `key: value` form."""


def parse_skill_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse SKILL.md frontmatter, enforcing the required `name` + `description`
    fields on top of the shared structural parser.

    The ONE strict parse behind every skill surface: the index (below), the
    CLI skill tooling (cli/commands/extensions/{skill,plugins,skill_package,_claude_code_plugin}.py
    import it as `meiva.skills._parse_frontmatter`), and the repo lint. Raises
    SkillFormatError on malformed frontmatter or a missing required field.
    """
    try:
        fields, body = parse_frontmatter(content)
    except FrontmatterError as e:
        raise SkillFormatError(str(e)) from e
    missing = [r for r in _REQUIRED_FIELDS if r not in fields]
    if missing:
        raise SkillFormatError("frontmatter missing required field(s): " + ", ".join(missing))
    return fields, body


@dataclass(frozen=True)
class SkillFile:
    """One skill-bearing folder under a scanned root.

    `name` / `description` / `error` are the parsed frontmatter outcome
    (exactly one of `error is None` and `name is None` holds for a folder with
    a SKILL.md); `content_hash` is the SHA-256 of the SKILL.md content for
    cross-root dedup; `index_text` carries a folder INDEX.md body so consumers
    never re-read it.
    """

    folder: Path
    rel: tuple[str, ...]
    skill_md: Path | None
    index_md: Path | None
    name: str | None
    description: str | None
    content_hash: str | None
    error: str | None
    index_text: str | None
    security_rules: tuple[str, ...]
    skill_mtime_ns: int
    skill_size: int
    index_mtime_ns: int
    index_size: int

    @property
    def key(self) -> str | None:
        """The comparison key: `match_key` of the frontmatter name. None when
        the entry carries no usable name (no SKILL.md or a parse error)."""
        return match_key(self.name) if self.name is not None else None


def _scan_critical(text: str) -> tuple[str, ...]:
    """Distinct critical rule ids matched in one skill text blob (SKILL.md or
    INDEX.md) — the runtime supply-chain gate. Empty tuple = clean. Sorted for
    deterministic messages."""
    from base.packages.skills.scan import scan_text_critical

    return tuple(sorted({f.rule_id for f in scan_text_critical(text)}))


def _read_utf8(path: Path) -> tuple[str | None, OSError | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, exc


def _parse_skill_md(
    skill_md: Path,
) -> tuple[str | None, str | None, str | None, str | None, tuple[str, ...]]:
    """`(name, description, content_hash, error, security_rules)` of one SKILL.md."""
    raw, err = _read_utf8(skill_md)
    if raw is None:
        return None, None, None, f"unreadable: {err}", ()
    content_hash = hashlib.sha256(raw.encode()).hexdigest()
    try:
        fields, _body = parse_skill_frontmatter(raw)
    except SkillFormatError as e:
        return None, None, content_hash, f"malformed frontmatter — {e}", ()
    return fields["name"], fields["description"], content_hash, None, _scan_critical(raw)


def _read_index_md(index_md: Path) -> str | None:
    """The INDEX.md text, or None when unreadable."""
    text, _ = _read_utf8(index_md)
    return text


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
    return st if st is not None and stat.S_ISREG(st.st_mode) else None


def _folders_in(
    root: Path, stat_of: Callable[[Path], os.stat_result | None]
) -> list[tuple[Path, int]] | None:
    folders: list[tuple[Path, int]] = []
    for p in sorted({root, *root.rglob("*")}):
        try:
            st = stat_of(p)
        except OSError:
            continue
        if st is not None and stat.S_ISDIR(st.st_mode):
            folders.append((p, st.st_mtime_ns))
    return folders or None


def _skill_file(
    folder: Path, root: Path, stat_of: Callable[[Path], os.stat_result | None]
) -> SkillFile | None:
    """Parse one folder's SKILL.md / INDEX.md; None when it holds neither."""
    rel = folder.relative_to(root).parts
    skill_md = folder / "SKILL.md"
    index_md = folder / "INDEX.md"
    s_stat = _regular_file_stat(skill_md, stat_of)
    i_stat = _regular_file_stat(index_md, stat_of)
    if s_stat is None and i_stat is None:
        return None
    name = description = content_hash = error = index_text = None
    security_rules: tuple[str, ...] = ()
    if s_stat is not None:
        name, description, content_hash, error, security_rules = _parse_skill_md(skill_md)
    if i_stat is not None:
        index_text = _read_index_md(index_md)
        if index_text is not None:
            security_rules += _scan_critical(index_text)
    return SkillFile(
        folder=folder,
        rel=rel,
        skill_md=skill_md if s_stat is not None else None,
        index_md=index_md if i_stat is not None else None,
        name=name,
        description=description,
        content_hash=content_hash,
        error=error,
        index_text=index_text,
        security_rules=security_rules,
        skill_mtime_ns=s_stat.st_mtime_ns if s_stat else 0,
        skill_size=s_stat.st_size if s_stat else 0,
        index_mtime_ns=i_stat.st_mtime_ns if i_stat else 0,
        index_size=i_stat.st_size if i_stat else 0,
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
    folder_stats = [(folder.relative_to(root).parts, mtime) for folder, mtime in folders]
    entries = [
        entry for folder, _ in folders if (entry := _skill_file(folder, root, stat_of)) is not None
    ]
    return tuple(entries), tuple(folder_stats)


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
        self._by_key: dict[str, SkillFile] = {}
        for e in entries:
            k = e.key
            if k is not None:
                self._by_key.setdefault(k, e)

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
            if folders is None:
                return False
            stats = [(folder.relative_to(root).parts, mtime) for folder, mtime in folders]
            if tuple(stats) != saved:
                return False
        for e in self.entries:
            for path, mtime, size in (
                (e.skill_md, e.skill_mtime_ns, e.skill_size),
                (e.index_md, e.index_mtime_ns, e.index_size),
            ):
                if path is None:
                    continue
                st = _stat_or_none(path)
                if st is None:
                    return False
                if st.st_mtime_ns != mtime or st.st_size != size:
                    return False
        return True

    def lookup(self, name: str) -> SkillFile | None:
        """The entry whose frontmatter name folds to `match_key(name)`, or
        None. First entry wins on a collision (the scan order is roots then
        sorted folders, the same order the runtime mount converges under)."""
        return self._by_key.get(match_key(name))


_CACHE: dict[tuple[Path, ...], SkillIndex] = {}
