# meiva:note
# Inspect the editable Meiva install in a checkout's virtualenv.
#
# uv writes `_editable_impl_meiva.pth` into the active virtualenv, and records the editable
# source URL in the matching `*.dist-info/direct_url.json`. A polluted `VIRTUAL_ENV` can
# therefore make a worktree install repoint a long-lived checkout's interpreter at disposable
# source. This module only reads the install: the exec boundary refuses to spawn a child from a
# violating interpreter and the health probe reports the violation. The few file stats are
# negligible beside a process spawn, and deliberately remain uncached so a newly poisoned install
# cannot evade the next execution. Nothing here rewrites an install; the fix for a violation is a
# locked `uv sync` run by the operator.
#
# The module is the reader: it lists and reads the records and resolves their paths. What the
# records must say, and how a violation is worded, is `editable_policy.py`, which judges the
# values read here. A failed sync that removes either the pointer or its `direct_url.json`
# metadata is a half-uninstall, not a missing-install no-op, and is reported as a violation.

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from base.deploy.release.editable_layout import (
    console_script_beside,
    layout_source_root,
    pointer_lines,
    pointers_beside,
    pointers_without_metadata,
    venv_python_candidates,
)
from base.deploy.release.editable_policy import (
    EditableReadings,
    MetadataRecord,
    PointerRecord,
    allowed_forms,
    editable_violations,
    exact_path_key,
    sorted_paths,
)

EDITABLE_PTH_NAME = "_editable_impl_meiva.pth"
EDITABLE_DIST_INFO_GLOB = "meiva-*.dist-info/direct_url.json"

# meiva:note
# Virtualenvs use `lib/pythonX.Y/site-packages` (or `lib64`). The layouts are scanned explicitly
# rather than derived from the running interpreter: stale Python-version directories are
# validated together rather than leaving a dormant poisoned pointer.
POINTER_PATTERNS = (
    f"lib/python*/site-packages/{EDITABLE_PTH_NAME}",
    f"lib64/python*/site-packages/{EDITABLE_PTH_NAME}",
)
DIRECT_URL_PATTERNS = (
    f"lib/python*/site-packages/{EDITABLE_DIST_INFO_GLOB}",
    f"lib64/python*/site-packages/{EDITABLE_DIST_INFO_GLOB}",
)

__all__ = [
    "EDITABLE_DIST_INFO_GLOB",
    "EDITABLE_PTH_NAME",
    "current_interpreter_source_root",
    "editable_console_script_violations",
    "editable_direct_url_paths",
    "editable_install_violations",
    "editable_meiva_pth_paths",
    "venv_python",
]


def _matches(venv: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    return sorted_paths(path for pattern in patterns for path in venv.glob(pattern))


# meiva:note
# Existing Meiva editable pointers under `source_root/.venv`, across every site-packages layout.
def editable_meiva_pth_paths(source_root: Path) -> tuple[Path, ...]:
    return _matches(source_root / ".venv", POINTER_PATTERNS)


# meiva:note
# Existing editable `direct_url.json` records under `source_root/.venv`: uv writes this file beside
# the editable pointer, so a poisoned install is recorded in both places and both must be asserted.
def editable_direct_url_paths(source_root: Path) -> tuple[Path, ...]:
    return _matches(source_root / ".venv", DIRECT_URL_PATTERNS)


# meiva:note
# Platform-native exact path identity of `path` as the filesystem resolves it.
def _normalized_exact_path(path: Path) -> str:
    return exact_path_key(path.resolve(strict=False))


def _path_exists(path: Path) -> bool:
    return path.exists()


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _read_pointer(path: Path) -> PointerRecord:
    text = _read(path)
    if text is None:
        return PointerRecord(path=path, text=None, targets=None)
    try:
        targets = tuple(_normalized_exact_path(Path(line)) for line in pointer_lines(text))
    except (OSError, ValueError):
        return PointerRecord(path=path, text=text, targets=None)
    return PointerRecord(path=path, text=text, targets=targets)


# meiva:note
# Observe every editable record of the checkout: the pointers and metadata that exist and are
# readable or not, the pointer paths missing beside existing metadata, and the pointers that have
# no metadata record in their site-packages directory.
def _read_editable_install(source_root: Path) -> EditableReadings:
    pointer_paths = editable_meiva_pth_paths(source_root)
    metadata_paths = editable_direct_url_paths(source_root)
    expected = pointers_beside(metadata_paths, EDITABLE_PTH_NAME)
    return EditableReadings(
        pointers=tuple(_read_pointer(path) for path in pointer_paths),
        metadata=tuple(MetadataRecord(path=path, text=_read(path)) for path in metadata_paths),
        pointers_missing=sorted_paths(path for path in expected if not _path_exists(path)),
        pointers_without_metadata=pointers_without_metadata(pointer_paths, metadata_paths),
    )


# meiva:note
# Human-readable allowlist violations of a checkout's editable install.
#
# Discovers every record (the `.pth` pointer and every `direct_url.json`) and validates them
# against the exact-root allowlist; it never writes. The health probe and the exec boundary report
# through this one function. A record that cannot be read is reported as a violation rather than
# crashing the caller.
#
# Returns an empty tuple when every record is legal. Both editable records missing is a no-op,
# while a missing pointer or metadata beside its existing sibling is a half-uninstall violation.
def editable_install_violations(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    if not source_root.is_absolute():
        raise ValueError(
            f"the checkout to inspect must be an absolute path, got {str(source_root)!r}"
        )
    resolved_source = source_root.resolve(strict=False)
    roots, urls = allowed_forms(
        resolved_source, [root.resolve(strict=False) for root in allowed_roots]
    )
    return editable_violations(
        _read_editable_install(resolved_source), allowed_roots=roots, allowed_urls=urls
    )


# meiva:note
# First existing checkout virtualenv interpreter.
def venv_python(source_root: Path) -> Path | None:
    for candidate in venv_python_candidates(source_root):
        if _path_exists(candidate):
            return candidate
    return None


# meiva:note
# Report a missing Meiva console script beside the virtualenv's interpreter.
def editable_console_script_violations(source_root: Path) -> tuple[str, ...]:
    interpreter = venv_python(source_root)
    if interpreter is None:
        return ()
    console_script, violation = console_script_beside(interpreter)
    if _path_exists(console_script):
        return ()
    return (violation,)


# meiva:note
# Return this interpreter's checkout root when it is under `.venv`.
#
# The supported layout is `<root>/.venv/bin/python`. A system interpreter or shim carries no
# checkout authority, so callers deliberately treat it as a no-op. A virtualenv's Python is
# normally a symlink to its base interpreter: the lexical executable path carries the `.venv`
# identity, and resolving it before the layout check would lose that boundary.
def current_interpreter_source_root(executable: str | Path) -> Path | None:
    source_root = layout_source_root(Path(executable))
    return None if source_root is None else source_root.resolve(strict=False)
