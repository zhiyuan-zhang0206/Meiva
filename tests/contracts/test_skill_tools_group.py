# meiva:note
# The `skill-tools` group is what the built-in skill scripts run; the application never
# imports it.
#
# `web-sources` extracts a page with trafilatura and parses a feed with feedparser;
# `audio-transcribe` and `web-sources:youtube` run the `yt-dlp` program. None of the three is
# part of the application's own import graph, so they live in a `skill-tools` dependency group
# instead of `[project.dependencies]`. `[tool.uv] default-groups` keeps every `uv sync` - a
# developer worktree, CI, a fleet update - installing the same set as before, and a face that
# runs no skill leaves the group out (`uv export --only-group svc-X`, or `--no-group
# skill-tools` on a one-machine build).
#
# The group is a promise, and these tests keep it checkable: every member declares the module
# name it would be imported as, no module outside `meiva_builtins/skills/` imports one, no
# member is a runtime dependency, the group stays a default group, a missing member raises the
# error that names the group, and the two service faces present carry or omit it as declared.
# meiva:link docs/conventions/dev-setup.md
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.skill_tools import GROUP, MissingSkillToolError, missing

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = frozenset(
    {".git", ".venv", ".worktrees", "node_modules", "__pycache__", ".cache", ".trunk"}
)

# meiva:note
# Distribution -> the module the member is imported as. A member a script runs as a program
# (`yt-dlp`) still ships an importable module (`yt_dlp`), so the scan covers it: a shipped
# module that started importing it would make the member a runtime dependency.
_MEMBERS = {
    "trafilatura": "trafilatura",
    "feedparser": "feedparser",
    "yt-dlp": "yt_dlp",
}

# meiva:note
# The directory whose scripts may use the group: the built-in skills. Everything else in the
# repository is application code.
_SKILLS_DIR = Path("meiva_builtins") / "skills"

# meiva:note
# One service face per service, in the order pyproject.toml lists them, with whether it runs
# the skill scripts.
_SERVICE_FACES = {
    "svc-agent-host": True,
    "svc-exec-host": False,
}


def _toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((_REPO_ROOT / relative).read_text(encoding="utf-8"))


def _distribution_name(requirement: str) -> str:
    # meiva:note
    # `psycopg[binary]>=3.2` -> `psycopg`: the name a group entry is compared by.
    return re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()


def _group(name: str) -> list[Any]:
    return _toml("pyproject.toml")["dependency-groups"][name]


def _declared(group: str) -> set[str]:
    # meiva:note
    # The distributions a group names itself; an `include-group` entry carries none.
    return {_distribution_name(item) for item in _group(group) if isinstance(item, str)}


def _includes(group: str, other: str) -> bool:
    entries = _group(group)
    return {"include-group": other} in entries or other in _declared(group)


def test_default_groups_install_the_group_beside_the_others() -> None:
    # meiva:note
    # This is the change's one new risk: drop `skill-tools` from the default set and every
    # `uv sync` - including the fleet update's `uv sync --frozen` - silently uninstalls the
    # group. The wave that lands such a change runs the *previous* checkout's
    # `switch_command`, so it cannot even name `--group` to put the packages back: every host
    # loses the skill scripts until the next update. A service face does not pay for the
    # default membership: `uv export --only-group svc-X` emits that group's closure only.
    defaults = _toml("pyproject.toml")["tool"]["uv"]["default-groups"]

    assert GROUP in defaults, (
        "a `uv sync` installs the default groups only: without `skill-tools` here, the next "
        "sync removes the skill scripts' libraries from every existing environment"
    )
    assert "dev" in defaults and "user-code" in defaults, (
        "the default set is 'dev' plus the two toolbox groups, never this group alone"
    )


def test_every_group_member_declares_its_import_name() -> None:
    declared = _declared(GROUP)

    assert declared == set(_MEMBERS), (
        "add or remove the member here with the module name a skill script imports; "
        f"missing={sorted(declared - set(_MEMBERS))} "
        f"stale={sorted(set(_MEMBERS) - declared)}"
    )


def test_the_group_is_not_a_runtime_dependency() -> None:
    runtime = {
        _distribution_name(item) for item in _toml("pyproject.toml")["project"]["dependencies"]
    }

    assert not (_declared(GROUP) & runtime), (
        "a library the application itself needs stays in [project.dependencies]; the group "
        "only holds what the skill scripts run"
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _python_files_outside_the_skills() -> list[Path]:
    # meiva:note
    # `rglob` would descend into each sibling worktree under `.worktrees/`, and the skills tree
    # is pruned at its own root instead of filtered per file.
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        here = Path(directory)
        if here.relative_to(_REPO_ROOT) == _SKILLS_DIR:
            dirnames[:] = []
            continue
        paths.extend(here / name for name in filenames if name.endswith(".py"))
    return paths


def _absolute_imports(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
    return found


def _group_imports(path: Path, wanted: tuple[str, ...]) -> list[tuple[str, int]]:
    # meiva:note
    # The text probe keeps thousands of files out of the parser; the AST then decides, so a
    # comment or a string that names the module is not a violation.
    source = _read(path)
    if source is None or not any(module in source for module in wanted):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        (want, line)
        for module, line in _absolute_imports(tree)
        for want in wanted
        if module == want or module.startswith(want + ".")
    ]


@cache
def _import_sites() -> dict[str, list[str]]:
    wanted = tuple(sorted(set(_MEMBERS.values())))
    sites: dict[str, list[str]] = {module: [] for module in wanted}
    for path in _python_files_outside_the_skills():
        for module, line in _group_imports(path, wanted):
            sites[module].append(f"{path.relative_to(_REPO_ROOT)}:{line}")
    return sites


@pytest.mark.parametrize(("distribution", "module"), sorted(_MEMBERS.items()))
def test_no_module_outside_the_skills_imports_a_member(distribution: str, module: str) -> None:
    importers = _import_sites()[module]

    assert not importers, (
        f"{distribution} is imported outside meiva_builtins/skills/, so it is a runtime "
        f"dependency rather than a skill tool; move it to [project.dependencies]: {importers}"
    )


def test_a_missing_member_names_the_group() -> None:
    # meiva:note
    # The message is the fix: an agent that meets this error has to learn the group's name and
    # the command from it, because the skill script is a subprocess it cannot inspect.
    error = missing("web-sources", "the `trafilatura` library")

    assert isinstance(error, MissingSkillToolError)
    assert GROUP in str(error)
    assert "uv sync --group" in str(error)


@pytest.mark.parametrize(("face", "carries"), sorted(_SERVICE_FACES.items()))
def test_service_faces_carry_or_omit_the_group_as_declared(face: str, carries: bool) -> None:
    assert face in _toml("pyproject.toml")["dependency-groups"], (
        f"{face} is the service face the comment on svc-agent-host names; keep it declared"
    )
    assert _includes(face, GROUP) is carries, (
        "agent-host runs the built-in skills and carries skill-tools; every other service "
        f"face leaves it out, and {face} says carries={carries}"
    )


def test_a_service_face_keeps_the_project_requirement_it_repeats() -> None:
    # meiva:note
    # The faces repeat a requirement the project already states (extras included: a bare
    # `psycopg` would drop the binary payload no import names). Repeating it is what makes a
    # face readable; agreeing with the project is what makes it correct.
    project = {
        _distribution_name(item): item
        for item in _toml("pyproject.toml")["project"]["dependencies"]
    }
    for face in _SERVICE_FACES:
        for item in _group(face):
            if not isinstance(item, str):
                continue
            name = _distribution_name(item)
            if name in project:
                assert item == project[name], (
                    f"{face} repeats {name} differently from [project.dependencies]; copy the "
                    "project's requirement so a bump lands in both places"
                )


def _exported(face: str) -> set[str]:
    # meiva:note
    # What an image built from this face installs: uv resolves the group's closure, extras
    # included, and prints it as `name==version` lines.
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH; the export face is exercised where a build runs")
    done = subprocess.run(  # noqa: S603
        [uv, "export", "--frozen", "--no-emit-project", "--no-header", "--only-group", face],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"uv export --only-group {face} failed: {done.stderr}"
    return {
        match.group(1).lower()
        for line in done.stdout.splitlines()
        if (match := re.match(r"^([A-Za-z0-9._-]+)==", line.strip()))
    }


@pytest.mark.parametrize(("face", "carries"), sorted(_SERVICE_FACES.items()))
def test_the_exported_face_follows_the_declaration(face: str, carries: bool) -> None:
    # meiva:note
    # The criterion the declaration is trusted on: `uv export --only-group` is what an image
    # installs, so the group's members have to be in one face's export and out of the other's.
    exported = _exported(face)
    present = sorted(_declared(GROUP) & exported)

    assert bool(present) is carries, (
        f"uv export --only-group {face} carries {present or 'none of'} skill-tools, "
        f"but the face is declared carries={carries}"
    )
