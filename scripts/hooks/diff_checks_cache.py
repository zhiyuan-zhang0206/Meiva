# meiva:note
# Sub-check verdict cache for `diff_checks.py` stored in `<git-common-dir>/diff-checks-cache-v1.json`.
# Only clean checkouts read or write the cache, and only passing sub-checks with readable outputs
# are persisted, so `git commit --amend` reuses unchanged checks without risking stale green results.

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard

CheckMode = Literal["incremental", "full"]

CACHE_VERSION = "diff-checks-cache/v1"
CACHE_FILENAME = "diff-checks-cache-v1.json"
MAX_CACHE_ENTRIES = 200
_MAX_OUTPUT_CHARS = 32_768
_PYTHON_CONFIG_FILES = ("pyproject.toml", "pyrightconfig.json", "uv.lock")
_PYTHON_CONFIG_SET = frozenset(_PYTHON_CONFIG_FILES)
_RUFF_CONFIG_FILES = ("pyproject.toml", "ruff.toml", ".ruff.toml", "uv.lock")


@dataclass(frozen=True)
class CacheContext:
    cache_path: Path
    head_tree: str
    base_sha: str
    python_digest: str


@dataclass(frozen=True)
class CachedRun:
    code: int
    output: str


@dataclass(frozen=True)
class CachedSelection:
    decision: str
    tests: tuple[str, ...]
    blind: tuple[str, ...]


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return is_object_list(value) and all(isinstance(item, str) for item in value)


def python_tree_digest(ls_tree_z: str) -> str:
    hasher = hashlib.sha256()
    for record in ls_tree_z.split("\0"):
        if not record or "\t" not in record:
            continue
        metadata, path = record.split("\t", 1)
        if path.endswith((".py", ".pyi")) or path in _PYTHON_CONFIG_SET:
            hasher.update(metadata.encode("utf-8"))
            hasher.update(b"\t")
            hasher.update(path.encode("utf-8"))
            hasher.update(b"\0")
    return hasher.hexdigest()


def files_digest(root: Path, paths: Sequence[str]) -> str:
    hasher = hashlib.sha256()
    seen: set[str] = set()
    for relative in paths:
        if relative in seen:
            continue
        seen.add(relative)
        target = root / relative
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        if target.is_file() and not target.is_symlink():
            try:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                digest = "unreadable"
            hasher.update(digest.encode("ascii"))
        else:
            hasher.update(b"missing")
        hasher.update(b"\n")
    return hasher.hexdigest()


def input_digest_for_run(
    context: CacheContext,
    root: Path,
    label: str,
    argv: Sequence[str],
) -> str:
    if label == "pyright":
        if len(argv) > 3:
            scoped = files_digest(root, (*_PYTHON_CONFIG_FILES, *argv[3:]))
            return f"{context.python_digest}:{scoped}"
        return context.python_digest
    if label == "ruff":
        if len(argv) > 2:
            return files_digest(root, (*_RUFF_CONFIG_FILES, *argv[2:]))
        return f"{context.python_digest}:{files_digest(root, _RUFF_CONFIG_FILES)}"
    if label in {"debt", "refusal-loss", "selector"}:
        return f"{context.head_tree}:{context.base_sha}"
    return context.head_tree


def cache_key(
    *,
    mode: CheckMode,
    label: str,
    argv: Sequence[str],
    cwd: str,
    input_state_digest: str,
) -> str:
    payload = json.dumps(
        [CACHE_VERSION, mode, label, list(argv), cwd, input_state_digest],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_cacheable_run(code: int, fields: Mapping[str, str]) -> bool:
    if code != 0 or fields.get("_over_baseline") == "1":
        return False
    return not any(value.startswith("unreadable") for value in fields.values())


def _load_entries(cache_path: Path) -> dict[str, dict[str, object]]:
    if not cache_path.is_file():
        return {}
    try:
        raw: object = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not is_str_object_dict(raw) or raw.get("version") != CACHE_VERSION:
        return {}
    entries_obj = raw.get("entries")
    if not is_str_object_dict(entries_obj):
        return {}
    return {key: value for key, value in entries_obj.items() if is_str_object_dict(value)}


def _store_entry(
    cache_path: Path,
    key: str,
    entry: dict[str, object],
    *,
    max_entries: int = MAX_CACHE_ENTRIES,
) -> None:
    entries = _load_entries(cache_path)
    entries.pop(key, None)
    entries[key] = entry
    while len(entries) > max_entries:
        del entries[next(iter(entries))]
    payload = json.dumps({"version": CACHE_VERSION, "entries": entries}, separators=(",", ":"))
    temp_path: Path | None = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=cache_path.parent,
            prefix=".diff-checks-cache-",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        temp_path.replace(cache_path)
    except OSError:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def lookup_run(context: CacheContext, key: str) -> CachedRun | None:
    entry = _load_entries(context.cache_path).get(key)
    if entry is None or entry.get("kind") != "run":
        return None
    code = entry.get("code")
    output = entry.get("output")
    if isinstance(code, int) and not isinstance(code, bool) and isinstance(output, str):
        return CachedRun(code=code, output=output)
    return None


def record_run(
    context: CacheContext,
    key: str,
    *,
    code: int,
    output: str,
    max_entries: int = MAX_CACHE_ENTRIES,
) -> None:
    _store_entry(
        context.cache_path,
        key,
        {"kind": "run", "code": code, "output": output[-_MAX_OUTPUT_CHARS:]},
        max_entries=max_entries,
    )


def lookup_selection(context: CacheContext, key: str) -> CachedSelection | None:
    entry = _load_entries(context.cache_path).get(key)
    if entry is None or entry.get("kind") != "selector":
        return None
    decision = entry.get("decision")
    tests = entry.get("tests")
    blind = entry.get("blind")
    if isinstance(decision, str) and _is_str_list(tests) and _is_str_list(blind):
        return CachedSelection(decision=decision, tests=tuple(tests), blind=tuple(blind))
    return None


def record_selection(
    context: CacheContext,
    key: str,
    *,
    decision: str,
    tests: Sequence[str],
    blind: Sequence[str],
    max_entries: int = MAX_CACHE_ENTRIES,
) -> None:
    _store_entry(
        context.cache_path,
        key,
        {"kind": "selector", "decision": decision, "tests": list(tests), "blind": list(blind)},
        max_entries=max_entries,
    )


def lookup_closure(context: CacheContext, python_files: Sequence[str]) -> set[str] | None:
    key = cache_key(
        mode="incremental",
        label="closure",
        argv=python_files,
        cwd="",
        input_state_digest=context.python_digest,
    )
    entry = _load_entries(context.cache_path).get(key)
    if entry is None or entry.get("kind") != "closure":
        return None
    paths = entry.get("paths")
    return set(paths) if _is_str_list(paths) else None


def record_closure(
    context: CacheContext,
    python_files: Sequence[str],
    reached: set[str],
    *,
    max_entries: int = MAX_CACHE_ENTRIES,
) -> None:
    key = cache_key(
        mode="incremental",
        label="closure",
        argv=python_files,
        cwd="",
        input_state_digest=context.python_digest,
    )
    _store_entry(
        context.cache_path,
        key,
        {"kind": "closure", "paths": sorted(reached)},
        max_entries=max_entries,
    )
