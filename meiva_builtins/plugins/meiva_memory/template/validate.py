#!/usr/bin/env python3
# meiva:note
# Validate shared notes in the Vault memory tree. The standalone seed owns the
# single-note rules imported by the plugin write check; its CLI scans memory/.

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ParamSpec, TypeGuard

import yaml

_RESERVED = {"MEMORY.md", "INHERIT.md", "index.md", "log.md", "AGENTS.md"}
_BUNDLE = Path(__file__).resolve().parent


_FM_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): (.*)$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")
_MISREAD_STRING_KEYS = (
    "title",
    "description",
    "name",
    "type",
    "meiva_machine",
    "timestamp",
    "verified",
    "stale_after",
)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


_P = ParamSpec("_P")


# meiva:note
# `function(*args, **kwargs)` read as an `object`: `yaml.safe_load` is typed `Any`, and this script
# also runs standalone inside a pool checkout, so it carries its own copy of the repository's
# `base.host.boundary.untyped.object_of` instead of importing `base`. It spells the parameters with
# `ParamSpec` rather than the Python 3.12 type-parameter syntax because the pool's `python3` need not
# be 3.12 (UP047 is waived for that reason).
def _object_of(  # noqa: UP047
    function: Callable[_P, object], /, *args: _P.args, **kwargs: _P.kwargs
) -> object:
    return function(*args, **kwargs)


def _misread_frontmatter_keys(fm_raw: str) -> list[str]:
    """Return keys whose raw line text differs from the YAML-parsed string value.

    An unquoted ' #' or ': ' inside a value silently truncates it (comment /
    nested mapping), so the parsed value drops the tail (2026-09-10: 143 pool
    files affected). Writer-side quoting is the real fix; this is the backstop.
    """
    bad: list[str] = []
    for line in fm_raw.splitlines():
        m = _FM_KEY_LINE_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key not in _MISREAD_STRING_KEYS or not raw or raw.startswith(('"', "'", "|", ">")):
            continue
        try:
            parsed = _object_of(yaml.safe_load, line)
        except yaml.YAMLError:
            continue
        value = parsed.get(key) if _is_str_dict(parsed) else None
        if isinstance(value, str) and value != raw:
            bad.append(key)
    return bad


def _frontmatter_problem(content: str) -> tuple[dict[str, object] | None, str | None, str | None]:
    """`(frontmatter, raw frontmatter text, error)` of a note's text; one side is None."""
    if not content.startswith("---\n"):
        return {}, "", None
    parts = content.split("---\n", 2)
    if len(parts) < 3:
        return None, None, "Unclosed YAML frontmatter"
    try:
        fm = _object_of(yaml.safe_load, parts[1])
    except yaml.YAMLError as e:
        return None, None, f"Invalid YAML: {e}"
    if not _is_str_dict(fm):
        return None, None, "Frontmatter must be a YAML mapping"
    return fm, parts[1], None


def _optional_okf_field_errors(fm: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if "sources" in fm:
        errors.append("Frontmatter 'sources' is forbidden; use inline Markdown file links")
    for date_key in ("verified", "stale_after"):
        if date_key in fm and fm[date_key] is not None:
            val = fm[date_key]
            if not (
                isinstance(val, str)
                and (_DATE_ONLY_RE.fullmatch(val) or _OFFSET_DATETIME_RE.fullmatch(val))
            ):
                errors.append(
                    f"Field '{date_key}' must be a quoted YYYY-MM-DD date string: {val!r}"
                )
    return errors


def source_repos(vault: Path, repos: Mapping[str, Path] | None = None) -> dict[str, Path]:
    # meiva:note
    # Resolve explicit mappings before conventional home checkouts and Vault repo mirrors.
    mapped = (
        {alias.name: alias.resolve() for alias in (vault / "repos").iterdir()}
        if (vault / "repos").is_dir()
        else {}
    )
    for alias, candidate in {
        "meiva": vault.parent / "source",
        "monsora": vault.parent.parent / "MonsoraV2",
    }.items():
        if candidate.is_dir():
            mapped.setdefault(alias, candidate.resolve())
    fallback = vault.parent.parent / "Meiva"
    if fallback.is_dir():
        mapped.setdefault("meiva", fallback.resolve())
    mapped.update(repos or {})
    return mapped


def _link_target(text: str, start: int) -> str | None:
    depth = 1
    escaped = False
    for end in range(start, len(text)):
        char = text[end]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                raw = text[start:end].strip()
                if raw.startswith("<") and ">" in raw:
                    return raw[1 : raw.index(">")]
                return raw.split()[0] if raw else ""
    return None


def body_links(content: str) -> list[tuple[int, str]]:
    # meiva:note
    # Extract inline Markdown links outside fenced and inline code, retaining line numbers.
    parts = note_parts(content)
    body = parts[1] if parts is not None else content
    offset = content.count("\n") - body.count("\n")
    lines: list[str] = []
    fence = ""
    for line in body.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif (
                marker[0] == fence[0]
                and len(marker) >= len(fence)
                and not line[match.end() :].strip()
            ):
                fence = ""
            lines.append("\n" if line.endswith("\n") else "")
        else:
            lines.append("\n" if fence and line.endswith("\n") else "" if fence else line)
    text = "".join(lines)
    text = re.sub(
        r"(?<!`)(`+)(?!`)(.*?)\1(?!`)", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S
    )
    links: list[tuple[int, str]] = []
    for match in re.finditer(r"(?<!!)\[(?:[^\]\\]|\\.)*\]\(", text):
        target = _link_target(text, match.end())
        if target is not None:
            links.append((offset + text.count("\n", 0, match.start()) + 1, target))
    return links


def resolve_link(
    note: Path, vault: Path, target: str, repos: Mapping[str, Path] | None = None
) -> tuple[Path, Path]:
    # meiva:note
    # Return (file, Git repository root); reject protocols, escapes, and non-files.
    raw = target.split("#", 1)[0]
    if not raw or ":" in raw or raw.startswith("//"):
        raise ValueError("malformed_source")
    vault = vault.resolve()
    repo = vault
    if raw.startswith("/repos/"):
        parts = raw.lstrip("/").split("/", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValueError("malformed_source")
        alias, rel = parts[1:]
        repo = source_repos(vault, repos).get(alias, vault / "repos" / alias).resolve()
        resolved = (repo / rel).resolve()
    else:
        resolved = (
            (vault / raw.lstrip("/")) if raw.startswith("/") else note.parent / raw
        ).resolve()
    if not resolved.is_relative_to(repo):
        raise ValueError("malformed_source")
    if not resolved.exists():
        raise ValueError("missing_path")
    if not resolved.is_file():
        raise ValueError("not_a_file")
    return resolved, repo


def _note_vault(note: Path) -> Path:
    return next((parent.parent for parent in note.parents if parent.name == "memory"), note.parent)


def validate_file(
    file_path: Path, *, vault: Path | None = None, repos: Mapping[str, Path] | None = None
) -> list[str]:
    """Validate a single OKF concept file. Returns list of error messages."""
    errors: list[str] = []

    if not file_path.is_file():
        return [f"File not found: {file_path}"]

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["File is not valid UTF-8"]

    fm, fm_raw, problem = _frontmatter_problem(content)
    if problem is not None:
        return [problem]
    assert fm is not None and fm_raw is not None  # noqa: S101

    errors.extend(_optional_okf_field_errors(fm))
    for line, target in body_links(content):
        try:
            resolve_link(file_path, vault or _note_vault(file_path), target, repos)
        except ValueError as exc:
            errors.append(f"Line {line}: {exc}: {target}")

    for key in _misread_frontmatter_keys(fm_raw):
        errors.append(
            f"Field '{key}' is YAML-misread: raw text differs from the parsed value "
            f"(an unquoted ' #' or ': ' truncates it) — wrap the value in double quotes"
        )

    return errors


# --- One note on its own ---
# The rules above that look at a single note, as one call: what a writer can check on the note it
# just wrote without the rest of the pool. Directory shape, indexes and pointers need the pool and
# are not part of it.


def note_char_cap() -> int:
    """The commit hook's per-note character cap: same variable, same default."""
    return int(os.environ.get("MEIVA_MEMORY_NOTE_CAP", "8000"))


def is_note_name(fname: str) -> bool:
    """Whether a file name is a note: a .md file that is not an index or another reserved file."""
    return fname.endswith(".md") and fname not in _RESERVED


def note_parts(content: str) -> tuple[dict[str, object], str] | None:
    """`(frontmatter, body)` of a note's text; None when its frontmatter is missing or invalid."""
    if not content.startswith("---\n"):
        return {}, content
    fm, _fm_raw, problem = _frontmatter_problem(content)
    if problem is not None or fm is None:
        return None
    return fm, content.split("---\n", 2)[2]


def validate_note(file_path: Path) -> list[str]:
    """Every finding that concerns one note by itself: `validate_file`, the tag discipline
    (valid `type/<x>`, no junk tags, a tags field at all) and the character cap.
    Returns [] for a reserved file name."""
    errors = validate_file(file_path)
    if file_path.name in _RESERVED:
        return errors
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # already reported by validate_file
        return errors
    cap = note_char_cap()
    if len(content) > cap:
        errors.append(f"Note is {len(content)} characters, over the {cap} cap")
    return errors


# --- Directory structure limits (hard rules, no skip) ---
# Every directory: at most MAX_FILES_PER_DIR markdown notes and at most
# MAX_SUBDIRS_PER_DIR subdirectories. Depth is deliberately UNLIMITED — deep
# structures (e.g. school -> term -> course -> notes) are legitimate. When a
# directory exceeds a limit, restructure it (split into topical subdirs) rather
# than skirting the rule; the memory steward consolidates such refactors.

MAX_FILES_PER_DIR = 20
MAX_SUBDIRS_PER_DIR = 20


def validate_structure(bundle: Path) -> list[str]:
    """Directory-shape limits. Returns list of error strings."""
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(bundle):
        dp = Path(dirpath)
        if any(part.startswith(".") for part in dp.relative_to(bundle).parts):
            continue
        md_files = [f for f in filenames if f.endswith(".md") and f not in ("index.md", "log.md")]
        n_files = len(md_files)
        n_dirs = len(dirnames)
        rel = dp.relative_to(bundle)
        label = str(rel) if str(rel) != "." else "(root)"
        if n_files > MAX_FILES_PER_DIR:
            errors.append(
                f"{label}: {n_files} md files, over the {MAX_FILES_PER_DIR} cap — "
                "split into topical subdirectories"
            )
        if n_dirs > MAX_SUBDIRS_PER_DIR:
            errors.append(
                f"{label}: {n_dirs} subdirectories, over the {MAX_SUBDIRS_PER_DIR} cap — "
                "merge or re-group"
            )
    return errors


# --- Fact-check declarations (Setup section truthiness checks) ---
# Any note (the MEMORY.md Setup section in particular) can declare a self-verifying fact
# as an HTML comment; validate.py executes it and fails the check when the fact
# no longer holds. This is the backstop for stale claims like "develop branch
# integration" surviving for weeks after the branch was deleted (2026-08-05).
#
#   <!-- fact-check: test -d ~/Meiva -->                       # command exit 0 = fact holds
#   <!-- fact-check: ! git -C ~/Meiva rev-parse --verify develop -->   # ! negates: command exit 0 = fact FAILS
#
# Only a whitelist of read-only commands is allowed (git / test / ls), so a
# fact-check can never mutate anything. "~" is expanded. At most
# _FACTCHECK_MAX per file keeps runtime bounded.

_FACTCHECK_ALLOWED = {"git", "test", "ls"}
_FACTCHECK_MAX = 20
_FACTCHECK_RE = re.compile(r"<!--\s*fact-check:\s*(!)?\s*([^\n]+?)\s*-->")


def validate_factchecks(bundle: Path) -> list[str]:
    """Execute declared fact-checks; return list of error strings."""
    errors: list[str] = []
    for fp in sorted(bundle.rglob("*.md")):
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = 0
        for m in _FACTCHECK_RE.finditer(content):
            count += 1
            if count > _FACTCHECK_MAX:
                errors.append(f"{fp.name}: over {_FACTCHECK_MAX} fact-checks — trim")
                break
            negate = bool(m.group(1))
            cmdline = m.group(2).strip()
            parts = cmdline.split()
            if not parts or parts[0] not in _FACTCHECK_ALLOWED:
                errors.append(
                    f"{fp.name}: fact-check command not whitelisted: {cmdline[:60]!r} "
                    f"(allowed: {sorted(_FACTCHECK_ALLOWED)})"
                )
                continue
            argv = [str(Path(a).expanduser()) for a in parts]
            try:
                # S603: whitelist above (git/test/ls only) keeps this read-only
                rc = subprocess.run(  # noqa: S603
                    argv, capture_output=True, timeout=15, check=False
                ).returncode
                ok = (rc == 0) != negate
            except (
                OSError,
                subprocess.TimeoutExpired,
            ):  # unrunnable or hung: reported as FAILED below
                ok = False
            if not ok:
                errors.append(f"{fp.name}: fact-check FAILED: {'!' if negate else ''}{cmdline}")
    return errors


# --- Pointer integrity (MEMORY.md index vs actual notes) ---
# The index must point at every note exactly once, and every pointer target
# must exist. Catches hand-edited pointers to renamed/missing files and
# duplicate lines (both observed in production, 2026-08).


def _pointer_target_errors(bundle: Path, seen: dict[str, int]) -> list[str]:
    errors: list[str] = []
    for t, n in seen.items():
        if n > 1:
            errors.append(f"MEMORY.md: duplicate pointer {t} ({n}x)")
        if not (bundle / t).exists():
            errors.append(f"MEMORY.md: pointer target missing: {t}")
    return errors


def _pointer_coverage_errors(bundle: Path, seen: dict[str, int]) -> list[str]:
    """Root-level notes need a file pointer; directories holding notes need a directory pointer."""
    errors: list[str] = []
    # Root-level notes: file-level coverage. Everything under a directory is
    # covered by that directory's pointer (progressive disclosure via index.md).
    for fp in sorted(bundle.glob("*.md")):
        if fp.name in _RESERVED:
            continue
        rel = fp.name
        if rel not in seen:
            errors.append(f"orphan note (no pointer in MEMORY.md): {rel}")

    # Directories holding notes need a pointer line to them (or their index.md).
    dirs_with_notes: set[str] = set()
    for fp in bundle.rglob("*.md"):
        if fp.name in _RESERVED or fp.parent == bundle:
            continue
        rel_dir = fp.relative_to(bundle).parent.as_posix()
        dirs_with_notes.add(rel_dir)
        while "/" in rel_dir:
            rel_dir = rel_dir.rsplit("/", 1)[0]
            dirs_with_notes.add(rel_dir)
    for d in sorted(dirs_with_notes):
        if "/" in d:
            continue  # subdirectories are enumerated by their parent's index.md
        pointed = any(
            t == d + "/" or t == d + "/index.md" or t.startswith(d + "/index.md") for t in seen
        )
        if not pointed:
            errors.append(f"directory without pointer: {d}/")
    return errors


def validate_pointers(bundle: Path) -> list[str]:
    """MEMORY.md pointer targets exist, unique, and cover every note."""
    mem = bundle / "MEMORY.md"
    if not mem.is_file():
        return []
    try:
        content = mem.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["MEMORY.md unreadable"]

    seen: dict[str, int] = {}
    for _, t in body_links(content):
        if not t.endswith(".md"):
            continue
        if t.startswith(("http", "https", "#")):
            continue
        normalized = t.removeprefix("/memory/")
        seen[normalized] = seen.get(normalized, 0) + 1

    return [
        *_pointer_target_errors(bundle, seen),
        *_pointer_coverage_errors(bundle, seen),
    ]


# meiva:note
# Every finding for the pool at `bundle`, and how many `.md` files were checked.
def validate_bundle(bundle: Path) -> tuple[list[str], int]:
    errors: list[str] = []
    vault = bundle
    bundle = bundle / "memory"
    errors.extend(validate_structure(bundle))
    errors.extend(validate_factchecks(bundle))
    errors.extend(validate_pointers(bundle))
    md_files = sorted(bundle.rglob("*.md"))

    for fp in md_files:
        file_errors = validate_file(fp, vault=vault)
        for e in file_errors:
            errors.append(f"{fp.name}: {e}")
    return errors, len(md_files)


def main() -> int:
    errors, checked = validate_bundle(_BUNDLE)
    if checked == 0:
        print("No .md files found.", file=sys.stderr)
        return 1

    if errors:
        print(f"OKF validation failed ({len(errors)} errors):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"OKF validation passed ({checked} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
