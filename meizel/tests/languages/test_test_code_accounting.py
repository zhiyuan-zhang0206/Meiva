from __future__ import annotations

from pathlib import Path

import pytest

from meizel.contracts.analysis import Analysis, exempt_test_modules
from meizel.graph.model import Fact, FactKind
from meizel.tests.synthetic import analysis_of

pytestmark = pytest.mark.registry

ROOT = Path(__file__).resolve().parents[3]

# meiva:note
# The doubles the rule withdraws, each with the records it holds and a production module beside it.
# The numbers are published in the checker document in the same change, so a repayment or a new site
# moves both; a ledger that drops without a line saying why is what this file refuses.
# meiva:link meizel/docs/checker/test-code.meiva.okf.md
DOCUMENTED = {
    "ui/web/src/app/page-test-mock.tsx": 101,
    "ui/web/src/components/agent-sidebar/sidebar-test-mock.tsx": 135,
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
# facts this tree produced. Injecting a production import is a fact appended to that list - no file is
# written and no checkout is made, which is how the manual reverse control becomes a CI test.
@pytest.fixture(scope="module")
def analysis() -> Analysis:
    return analysis_of(ROOT)


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
