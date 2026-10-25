"""The import statements of the non-test source, cached per file.

Placement (`scripts/structure/placement.py`) needs the direct import edges of the production
code: which package's subtree imports which module. Reading them means parsing ~1500 files
(1.3-2.5 s), too slow for a per-file pre-commit run, so each file's import statements are
kept in `.cache/structure/production-imports.json`, keyed on the file's `(mtime_ns, size)`.
A changed file re-parses itself and a deleted one drops out, so the cache can never be stale
and is never committed.

What is cached is the file's own text only: its first-party import statements with relative
imports made absolute. Which name in `from a import b` is a submodule depends on the rest of
the tree, so that resolution is not cached; the caller resolves the statements against the
current checkout with the shared clause resolver (`scripts.structure.imports.dependencies`).

The scope is that of `UnitGraph.empirical`: every `.py` under the code tops except files
below a `tests` or `docs` directory.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeGuard

from scripts.structure import imports

__all__ = ["CACHE_PATH", "production_imports"]

CACHE_PATH = ".cache/structure/production-imports.json"
_VERSION = 3
_SKIPPED_DIRS = frozenset({"tests", "docs", "__pycache__"})

Entry = tuple[int, int, str]  # (mtime_ns, size, import statements, one per line)


def production_imports(
    repo_root: Path,
    tops: Sequence[str],
    *,
    read_source: Callable[[Path], str],
    cache_path: str | Path = CACHE_PATH,
) -> dict[str, str]:
    """Repo-relative path -> the file's first-party import statements (one per line)."""
    cache_file = Path(cache_path) if Path(cache_path).is_absolute() else repo_root / cache_path
    cached = _load(cache_file)
    found: dict[str, Entry] = {}
    for rel, path in _source_files(repo_root, tops):
        stat = path.stat()
        hit = cached.get(rel)
        if hit is not None and hit[:2] == (stat.st_mtime_ns, stat.st_size):
            found[rel] = hit
        else:
            found[rel] = (
                stat.st_mtime_ns,
                stat.st_size,
                _statements(path, rel, tops, read_source=read_source),
            )
    if found != cached:
        _store(cache_file, found)
    return {rel: entry[2] for rel, entry in found.items()}


def _source_files(repo_root: Path, tops: Sequence[str]) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for top in tops:
        for directory, subdirs, names in os.walk(repo_root / top):
            subdirs[:] = sorted(d for d in subdirs if d not in _SKIPPED_DIRS)
            for name in sorted(names):
                if name.endswith(".py"):
                    path = Path(directory, name)
                    files.append((path.relative_to(repo_root).as_posix(), path))
    return files


def _statements(
    path: Path,
    rel: str,
    tops: Sequence[str],
    *,
    read_source: Callable[[Path], str],
) -> str:
    """The absolute first-party import statements of one file, function-level ones included."""
    try:
        tree = ast.parse(read_source(path))
    except (SyntaxError, UnicodeDecodeError):
        return ""  # unparsable members are skipped, as every structure lint skips them
    lines: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            clause = imports.normalize(node, rel)
            if any(target.split(".")[0] in tops for target in clause.candidates):
                lines.append(clause.statement)
    return "\n".join(lines)


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _entry(value: object) -> Entry | None:
    if not _is_object_list(value) or len(value) != 3:
        return None
    mtime_ns, size, statements = value[0], value[1], value[2]
    if type(mtime_ns) is int and type(size) is int and isinstance(statements, str):
        return (mtime_ns, size, statements)
    return None


def _load(cache_file: Path) -> dict[str, Entry]:
    """The cached entries; a missing, unreadable or foreign-version cache is simply empty."""
    try:
        data: object = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not _is_str_object_dict(data):
        return {}
    files = data.get("files")
    if data.get("version") != _VERSION or not _is_str_object_dict(files):
        return {}
    result: dict[str, Entry] = {}
    for rel, raw_entry in files.items():
        parsed = _entry(raw_entry)
        if parsed is None:
            return {}
        result[rel] = parsed
    return result


def _store(cache_file: Path, entries: dict[str, Entry]) -> None:
    """Replace the cache atomically; a checkout that cannot be written just runs uncached."""
    payload = json.dumps({"version": _VERSION, "files": entries}, separators=(",", ":"))
    temporary: Path | None = None
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_file.parent,
            prefix=f"{cache_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        temporary.replace(cache_file)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
