# meiva:note
# What the skill index decides from what it was handed, as values: the strict SKILL.md frontmatter
# parse behind every skill surface, the facts one SKILL.md yields (name, description, content hash,
# error, the critical scan rules), how a folder's stats and texts assemble into a `SkillFile`, and
# whether a re-stat of a scanned tree still matches what an index saved. `index.py` walks the
# tree, stats and reads the files and keeps the process cache.

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from base.packages.docs.frontmatter import FrontmatterError, parse_frontmatter
from base.packages.skills.names import match_key
from base.packages.skills.scan import scan_text_critical

_REQUIRED_FIELDS = ("name", "description")
# meiva:note
# The two files of a skill-bearing folder.
SKILL_MD = "SKILL.md"
INDEX_MD = "INDEX.md"


# meiva:note
# SKILL.md failed to parse — missing frontmatter, missing required `name` or `description` field,
# or a field is not in `key: value` form.
class SkillFormatError(ValueError):
    pass


# meiva:note
# Parse SKILL.md frontmatter, enforcing the required `name` + `description` fields on top of the
# shared structural parser. The ONE strict parse behind every skill surface: the index, the CLI
# skill tooling (cli/commands/extensions/{skill,plugins,skill_package,_claude_code_plugin}.py
# import it as `meiva.skills._parse_frontmatter`), and the repo lint. Raises SkillFormatError on
# malformed frontmatter or a missing required field.
def parse_skill_frontmatter(content: str) -> tuple[dict[str, str], str]:
    try:
        fields, body = parse_frontmatter(content)
    except FrontmatterError as e:
        raise SkillFormatError(str(e)) from e
    missing = [r for r in _REQUIRED_FIELDS if r not in fields]
    if missing:
        raise SkillFormatError("frontmatter missing required field(s): " + ", ".join(missing))
    return fields, body


# meiva:note
# One skill-bearing folder under a scanned root. `name` / `description` / `error` are the parsed
# frontmatter outcome (exactly one of `error is None` and `name is None` holds for a folder with a
# SKILL.md); `content_hash` is the SHA-256 of the SKILL.md content for cross-root dedup;
# `index_text` carries a folder INDEX.md body so consumers never re-read it.
@dataclass(frozen=True)
class SkillFile:
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

    # meiva:note
    # The comparison key: `match_key` of the frontmatter name. None when the entry carries no usable
    # name (no SKILL.md or a parse error).
    @property
    def key(self) -> str | None:
        return match_key(self.name) if self.name is not None else None


# meiva:note
# Distinct critical rule ids matched in one skill text blob (SKILL.md or INDEX.md) — the runtime
# supply-chain gate. Empty tuple = clean. Sorted for deterministic messages.
def _scan_critical(text: str) -> tuple[str, ...]:
    return tuple(sorted({f.rule_id for f in scan_text_critical(text)}))


# meiva:note
# What one SKILL.md yields: the frontmatter outcome, the content hash for cross-root dedup and the
# critical rules its text matches.
@dataclass(frozen=True)
class SkillMdFacts:
    name: str | None
    description: str | None
    content_hash: str | None
    error: str | None
    security_rules: tuple[str, ...]


# meiva:note
# The facts of one SKILL.md from its text, or from the error that kept it from being read.
def skill_md_facts(raw: str | None, unreadable: OSError | None) -> SkillMdFacts:
    if raw is None:
        return SkillMdFacts(None, None, None, f"unreadable: {unreadable}", ())
    content_hash = hashlib.sha256(raw.encode()).hexdigest()
    try:
        fields, _body = parse_skill_frontmatter(raw)
    except SkillFormatError as e:
        return SkillMdFacts(None, None, content_hash, f"malformed frontmatter — {e}", ())
    return SkillMdFacts(
        fields["name"], fields["description"], content_hash, None, _scan_critical(raw)
    )


# meiva:note
# What a stat says an entry is: a regular file, a directory, something else (a symlink, a socket),
# or (no stat) missing. `FileKind.of` reads it off a stat.
class FileKind(Enum):
    REGULAR = "regular"
    DIRECTORY = "directory"
    OTHER = "other"
    MISSING = "missing"

    @classmethod
    def of(cls, st: os.stat_result | None) -> FileKind:
        if st is None:
            return cls.MISSING
        if stat.S_ISREG(st.st_mode):
            return cls.REGULAR
        if stat.S_ISDIR(st.st_mode):
            return cls.DIRECTORY
        return cls.OTHER


# meiva:note
# The `SkillFile` of one folder from the stats of its SKILL.md and INDEX.md (None for a file that
# is not a regular file), the facts of the SKILL.md and the text of the INDEX.md; None when the
# folder holds neither. A critical rule in the INDEX.md joins the rules of the SKILL.md.
def assemble_skill_file(
    folder: Path,
    root: Path,
    *,
    skill_stat: os.stat_result | None,
    index_stat: os.stat_result | None,
    skill_facts: SkillMdFacts | None,
    index_text: str | None,
) -> SkillFile | None:
    if skill_stat is None and index_stat is None:
        return None
    if (skill_stat is None) != (skill_facts is None):
        raise ValueError("the facts of a SKILL.md come with its stat and only with it")
    facts = skill_facts or SkillMdFacts(None, None, None, None, ())
    security_rules = facts.security_rules
    if index_stat is not None and index_text is not None:
        security_rules += _scan_critical(index_text)
    return SkillFile(
        folder=folder,
        rel=folder.relative_to(root).parts,
        skill_md=folder / SKILL_MD if skill_stat is not None else None,
        index_md=folder / INDEX_MD if index_stat is not None else None,
        name=facts.name,
        description=facts.description,
        content_hash=facts.content_hash,
        error=facts.error,
        index_text=index_text if index_stat is not None else None,
        security_rules=security_rules,
        skill_mtime_ns=skill_stat.st_mtime_ns if skill_stat else 0,
        skill_size=skill_stat.st_size if skill_stat else 0,
        index_mtime_ns=index_stat.st_mtime_ns if index_stat else 0,
        index_size=index_stat.st_size if index_stat else 0,
    )


# meiva:note
# The folders of a scanned root as (parts relative to the root, mtime) pairs: what an index saves
# to tell, later, that a folder appeared, vanished or changed.
def folder_snapshot(
    root: Path, folders: list[tuple[Path, int]]
) -> tuple[tuple[tuple[str, ...], int], ...]:
    return tuple((folder.relative_to(root).parts, mtime) for folder, mtime in folders)


# meiva:note
# Whether a file still matches what an index saved of it: it exists and has the saved mtime and
# size. A missing stat reads as changed.
def file_unchanged(st: os.stat_result | None, *, mtime_ns: int, size: int) -> bool:
    return st is not None and st.st_mtime_ns == mtime_ns and st.st_size == size


# meiva:note
# The entries that carry a usable name, keyed by the comparison key of that name. The first entry
# wins on a collision: the scan order is roots, then sorted folders, the same order the runtime
# mount converges under.
def key_index(entries: Sequence[SkillFile]) -> dict[str, SkillFile]:
    by_key: dict[str, SkillFile] = {}
    for entry in entries:
        key = entry.key
        if key is not None:
            by_key.setdefault(key, entry)
    return by_key
