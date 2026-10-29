from __future__ import annotations

import tomllib
from pathlib import Path

from meizel import lint_common
from meizel.roles import packages

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_framework_dirs_are_the_declared_packages() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(wheel) == set(packages("application"))
    assert packages("application", "standalone") == lint_common.FRAMEWORK_DIRS


def test_root_environment_regions_do_not_admit_independent_projects() -> None:
    from meizel.contracts.enumerations import parse_regions

    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    regions = parse_regions({"pyproject.toml": text})
    assert [(region.format, region.roles) for region in regions] == [
        ("names", ("application",)),
        ("testpaths", ("tests", "application", "tooling", "templates")),
        ("names", ("application", "frontend")),
        ("names", ("application", "tooling", "tests")),
    ]
    declared = {
        "application": ("core",),
        "standalone": ("engines",),
        "tests": ("tests",),
        "tooling": ("tools",),
        "templates": ("jobs",),
        "frontend": ("web",),
    }
    assert all("engines" not in line for region in regions for line in region.render(declared))
    assert packages("application", "standalone") == lint_common.FRAMEWORK_DIRS
