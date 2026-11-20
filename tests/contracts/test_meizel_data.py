from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import meizel
import pytest
from meizel import allowances
from meizel.contracts.analysis import Analysis, analyze, exempt_test_modules, load_policy
from meizel.entry import main, open_repository
from meizel.graph.model import Fact, FactKind
from meizel.language.scan import typescript_files
from meizel.roles import framework_dirs, parse_roles, select_packages

# meiva:note
# This repository's own facts as the locality checker reads them. meizel is an installed dependency
# whose rules are tested in its own repository; what is tested here is this repository's data: the
# roles table against the lists other files keep, the policy's allowance counts, the approved patch
# sites, and which test doubles the TypeScript accounting withdraws. The readers walk the whole tree,
# so the file is marked `registry` and the selector always runs it. `_CHECKER` is the installed
# checker's own source, which must hold no exemption of this repository's.
pytestmark = pytest.mark.registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = _REPO_ROOT
_CHECKER = Path(meizel.__file__).resolve().parent

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


# meiva:note
# An approved file keeps its patches out of every baseline, so nothing else stops one more from being
# added: the number of patch sites each holds is pinned here, and a change to it (or to the list) is a
# deliberate edit that a reviewer reads. `python -m meizel debt approved` prints these numbers. The
# TypeScript file needs the compiler, which is why this test is in a file of the native lane.
APPROVED_PATCH_SITES = {
    "cli/tests/bootstrap/child_main_dispatch.py": 2,
    "tests/e2e/fakes/child_budget_writer.py": 2,
    "tests/fixtures/guards.py": 7,
    "ui/web/tests/vitest.setup.ts": 1,
}


def test_the_patch_sites_of_the_approved_files_are_pinned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--repo", str(ROOT), "--jobs", "1", "debt", "approved", "--json"]) == 0
    found = {item["path"]: item["sites"] for item in json.loads(capsys.readouterr().out)}
    assert found == APPROVED_PATCH_SITES


# meiva:note
# `Clock.from_settings()` builds the process clock from the process settings: the read the root
# declares is `settings`, and the clock it returns runs on the system timers, so it is not a second
# declaration of `clock`. Taking it out of `settings` would put the declarations of its callers back.
def test_the_process_clock_constructor_is_a_settings_read() -> None:
    policy = allowances.load(open_repository(ROOT))
    reader = "base.clock.Clock.from_settings()"
    assert reader in policy.reasons("ambient-readers", "settings")
    assert reader not in policy.reasons("ambient-readers", "clock")


def test_no_rule_code_holds_an_exemption_of_its_own() -> None:
    policy = allowances.load(open_repository(ROOT))
    exempt = {name for entries in policy.listed.values() for name in entries if "/" in name}
    assert len(exempt) > 50
    literals = {
        node.value: path.relative_to(_CHECKER).as_posix()
        for path in _CHECKER.rglob("*.py")
        if "tests" not in path.relative_to(_CHECKER).parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {name: literals[name] for name in exempt & literals.keys()} == {}


# meiva:note
# The allowance tables are a review surface, not a convenience: each entry says one file may do what
# the rule otherwise forbids. Pinning the counts makes adding one a deliberate edit here too, so a
# new allowance cannot arrive inside a change that looks local. Bump a number only with its entry.
def test_repo_policy_allowance_counts_are_pinned() -> None:
    policy = allowances.load(open_repository(ROOT))
    assert len(policy.external) == 31
    assert len(policy.dynamic_attrs) == 33
    assert len(policy.loads) == 23


# meiva:note
# The doubles the rule withdraws, each with the records it holds and a production module beside it.
# The numbers are published in the checker document in the same change, so a repayment or a new site
# moves both; a ledger that drops without a line saying why is what this file refuses.
DOCUMENTED = {
    "ui/web/src/app/page-test-mock.tsx": 111,
    "ui/web/src/components/agent-sidebar/sidebar-test-mock.tsx": 141,
    "ui/web/src/components/inspector/inspector-test-mock.tsx": 4,
}
PRODUCTION = {
    "ui/web/src/app/page-test-mock.tsx": "ui/web/src/app/page.tsx",
    "ui/web/src/components/agent-sidebar/sidebar-test-mock.tsx": (
        "ui/web/src/components/agent-sidebar/index.tsx"
    ),
    "ui/web/src/components/inspector/inspector-test-mock.tsx": (
        "ui/web/src/components/inspector/inspector-panel.tsx"
    ),
}


# meiva:note
# One analysis for the file: the rule reads the run's import graph, so every case below works on the
# facts this tree produced. Python AST scans never emit import or patch facts targeting TypeScript
# paths (and manifest `[loads]` are still loaded in full), so when every documented double is
# TypeScript, scoping `changed` to `typescript_files` yields the exact same exemption set, patch
# sites, and diagnostics without scanning Python files. Injecting a production import is a fact
# appended to that list - no file is written and no checkout is made, which is how the manual reverse
# control becomes a CI test.
@pytest.fixture(scope="module")
def analysis() -> Analysis:
    universe = open_repository(ROOT).universe()
    policy, _ = load_policy(universe)
    scope = (
        None
        if any(module.endswith(".py") for module in DOCUMENTED)
        else typescript_files(universe, policy)
    )
    return analyze(universe, changed=scope)


def _sites(analysis: Analysis) -> dict[str, int]:
    counts = dict.fromkeys(DOCUMENTED, 0)
    for scan in analysis.scans.values():
        for patch in scan.patches:
            if patch.code.value != "first-party-patch":
                continue
            for module in DOCUMENTED:
                if patch.target == module or patch.target.startswith(f"{module}."):
                    counts[module] += 1
    return counts


def test_each_documented_double_is_test_code_by_the_runs_import_facts(analysis: Analysis) -> None:
    modules = exempt_test_modules(analysis.facts)
    assert [module for module in DOCUMENTED if module not in modules] == []


def test_the_exemption_withdraws_exactly_the_documented_records(analysis: Analysis) -> None:
    assert _sites(analysis) == DOCUMENTED
    recorded = [
        diagnostic
        for diagnostic in analysis.diagnostics
        if diagnostic.code.value == "first-party-patch"
        and any(f" {module}" in diagnostic.message for module in DOCUMENTED)
    ]
    assert recorded == []


@pytest.mark.parametrize("module", sorted(DOCUMENTED))
def test_a_production_import_brings_the_doubles_records_back(
    analysis: Analysis, module: str
) -> None:
    imported = Fact(PRODUCTION[module], module, 1, FactKind.IMPORT)
    assert module in exempt_test_modules(analysis.facts)
    assert module not in exempt_test_modules([*analysis.facts, imported])


@pytest.mark.parametrize("module", sorted(DOCUMENTED))
def test_an_import_by_another_test_keeps_the_double_test_only(
    analysis: Analysis, module: str
) -> None:
    importer = next(path for path in DOCUMENTED if path != module)
    imported = Fact(importer, module, 1, FactKind.IMPORT)
    assert module in exempt_test_modules([*analysis.facts, imported])
