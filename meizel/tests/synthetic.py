"""Build small throwaway repositories and read the checker's verdict on them."""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from textwrap import dedent
from typing import TypeGuard

from meizel.contracts.analysis import POLICY, Analysis, DebtEntry, analyze, render_policy
from meizel.entry import main, open_repository
from meizel.graph.model import Diagnostic
from meizel.graph.universe import Universe
from meizel.state.ambient_state.tests.conftest import (
    synthetic_ambient_allowlists as synthetic_ambient_allowlists,
)

type RepoFactory = Callable[[Mapping[str, str]], Path]

STANDALONE_PROJECT = (
    '[build-system]\nbuild-backend = "hatchling.build"\n'
    '[tool.hatch.build.targets.wheel]\npackages = ["src/meiva_embedding_service"]\n'
    "[project.optional-dependencies]\ncpu = []\n"
    '[tool.basedpyright]\ntypeCheckingMode = "strict"\ninclude = ["src", "tests"]\n'
    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
)


# meiva:note
# The counts a baseline table records, one per key: an entry also carries the owner paying it down and
# the condition it waits for, and a test that judges only the number says so here.
def counts(table: Mapping[str, DebtEntry]) -> dict[str, int]:
    return {key: entry.count for key, entry in table.items()}


# meiva:note
# The metadata a fixture entry carries, written as values no real one can be mistaken for: a test
# entry exists only for the test that reads it, so its owner says `synthetic` rather than borrowing a
# task id that would outlive the fixture and answer for entries that never existed.
FIXTURE_OWNER = "synthetic"
FIXTURE_UNTIL = "permanently: a fixture entry that exists only for the test that reads it"


# meiva:note
# One `[debt.<code>]` table, entries in the order given: what a fixture writes instead of repeating
# the two fields a recorded entry needs on every line.
def baseline(code: str, rows: Mapping[str, int]) -> str:
    lines = "\n".join(f'"{key}" = {entry_text(count)}' for key, count in rows.items())
    return f"[debt.{code}]\n{lines}\n"


def entry_text(count: int) -> str:
    return f'{{ count = {count}, owner = "{FIXTURE_OWNER}", until = "{FIXTURE_UNTIL}" }}'


# meiva:note
# `debt record` as a test drives it: with the fixture metadata, so the tree it writes is one `check`
# accepts. A test about the placeholder `record` writes on its own calls the command directly.
def record(root: Path, *targets: str) -> int:
    return run(root, "debt", "record", *targets, "--owner", FIXTURE_OWNER, "--until", FIXTURE_UNTIL)


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def listed_allowances(files: Mapping[str, str]) -> str:
    """A policy listing every `[loads]` and `[external]` site the given manifests declare."""
    external: dict[str, str] = {}
    loads: dict[str, tuple[str, ...]] = {}
    for rel, text in files.items():
        if rel.rsplit("/", 1)[-1] != "meizel.toml":
            continue
        directory = rel.rpartition("/")[0]
        try:
            document = tomllib.loads(dedent(text).lstrip("\n"))
        except tomllib.TOMLDecodeError:
            continue  # a malformed manifest is the test's subject, not an allowance
        external_table: object = document.get("external", {})
        loads_table: object = document.get("loads", {})
        if not _is_table(external_table) or not _is_table(loads_table):
            continue
        for name, reason in external_table.items():
            external[f"{directory}/{name}" if directory else name] = str(reason)
        for name, targets in loads_table.items():
            if _is_str_list(targets):
                loads[f"{directory}/{name}" if directory else name] = tuple(targets)
    return render_policy(external, loads)


def build_repo(base: Path, files: Mapping[str, str]) -> Path:
    """A throwaway repository; unless it brings its own policy, one lists its allowances."""
    root = base / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", "-q", str(root)], check=True)  # noqa: S603 - fixed argv
    given = dict(files)
    if POLICY not in given:
        given[POLICY] = listed_allowances(files)
    for rel, text in given.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603 - fixed argv
    return root


# meiva:note
# The repository seat the checker reads a synthetic checkout through.
def seat(root: Path) -> Universe:
    return open_repository(root).universe()


def analysis_of(root: Path) -> Analysis:
    return analyze(seat(root), jobs=1)


def verdict(root: Path) -> set[tuple[str, str]]:
    """(code, path) of every diagnostic."""
    return {(d.code.value, d.path) for d in analysis_of(root).diagnostics}


def diagnostics(root: Path, code: str) -> list[Diagnostic]:
    return [d for d in analysis_of(root).diagnostics if d.code.value == code]


def edges(root: Path) -> set[tuple[str, str, str]]:
    """(src, dst, kind) of every fact edge."""
    return {(f.src, f.dst, f.kind.value) for f in analysis_of(root).facts}


def failures(root: Path) -> set[tuple[str, str]]:
    """(code, path) of every diagnostic that fails `check`, recorded debt taken into account."""
    return {(d.code.value, d.path) for d in analysis_of(root).verdict}


def run(root: Path, *args: str) -> int:
    """One `python -m meizel` invocation against `root`."""
    return main(["--repo", str(root), "--jobs", "1", *args])


def write(root: Path, files: Mapping[str, str]) -> None:
    """Add or replace files and stage them, so the universe sees them."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    git(root, "add", "-A")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603 - fixed argv


def commit(root: Path, message: str = "snapshot") -> None:
    git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
