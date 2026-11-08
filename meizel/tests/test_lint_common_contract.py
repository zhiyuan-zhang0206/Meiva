from __future__ import annotations

import tomllib
from pathlib import Path

from meizel.roles import framework_dirs, parse_roles, select_packages

_REPO_ROOT = Path(__file__).resolve().parents[2]

# meiva:note
# The roles this repository declares, parsed from the manifest's text: the decision is the model's,
# the read is the test's own.
ROLES = parse_roles((_REPO_ROOT / "meizel.toml").read_text(encoding="utf-8"), label="meizel.toml")


def test_framework_dirs_are_the_declared_packages() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(wheel) == set(select_packages(ROLES, "application"))
    assert set(wheel) <= set(framework_dirs(ROLES))


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
