# meiva:note
# Where the records of a checkout's editable install and its interpreter live, as paths and
# values: the pointer lines to resolve, the pointer that sits beside a metadata record, the
# pointers that have none, the interpreters and console script of a virtualenv and the
# checkout an interpreter path belongs to. `editable_install.py` lists, reads and resolves them and
# `editable_policy.py` judges what it read.

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from base.deploy.release.editable_policy import sorted_paths


# meiva:note
# The non-blank lines of an editable pointer: each names a source to resolve.
def pointer_lines(text: str) -> tuple[str, ...]:
    return tuple(line for line in text.splitlines() if line)


# meiva:note
# The editable pointers the metadata records say must exist: uv writes `direct_url.json` in a
# `*.dist-info` directory beside the pointer, in the same site-packages directory.
def pointers_beside(metadata_paths: Iterable[Path], pointer_name: str) -> frozenset[Path]:
    return frozenset(record.parent.parent / pointer_name for record in metadata_paths)


# meiva:note
# The pointers whose site-packages directory holds no metadata record: a metadata record sits in a
# `*.dist-info` directory in the same directory as its pointer, so a pointer is without one when no
# record's `dist-info` parent is its directory. Sorted and distinct.
def pointers_without_metadata(
    pointer_paths: Iterable[Path], metadata_paths: Iterable[Path]
) -> tuple[Path, ...]:
    site_packages = {record.parent.parent for record in metadata_paths}
    return sorted_paths(p for p in pointer_paths if p.parent not in site_packages)


# meiva:note
# The interpreters of a checkout's virtualenv, in the order they are preferred.
def venv_python_candidates(source_root: Path) -> tuple[Path, Path]:
    venv = source_root / ".venv"
    return venv / "bin" / "python3", venv / "bin" / "python"


# meiva:note
# The console script that must sit beside an interpreter, and the violation to report when it does
# not.
def console_script_beside(interpreter: Path) -> tuple[Path, str]:
    console_script = interpreter.with_name("meiva")
    return console_script, f"{console_script} editable console script missing"


# meiva:note
# The checkout root an interpreter path belongs to, as written, or None when the path is not
# `<root>/.venv/bin/<python>`. A virtualenv's Python is normally a symlink to its base interpreter:
# the lexical path carries the `.venv` identity, so this reads the path as given and never
# resolves it.
def layout_source_root(interpreter: Path) -> Path | None:
    try:
        venv = interpreter.parents[1]
        source_root = interpreter.parents[2]
    except IndexError:
        return None
    if venv.name != ".venv" or interpreter.parent.name != "bin":
        return None
    return source_root
