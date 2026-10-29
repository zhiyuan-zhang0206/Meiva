from __future__ import annotations

import pytest

from meizel.language.fixtures import is_test_file

# meiva:note
# The patch exemption reads this convention, so the pattern is the review surface: a new name is
# exempt the moment it matches, with no list to update. The accepted classes are therefore stated
# here beside the near misses they refuse; widening one - a bare `-mock`, a `-fixtures-mock`, a
# `-fake` - fails this table until someone edits it on purpose, which is what a widened exemption
# has to look like in review.
# meiva:link ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md
ACCEPTED = [
    "pkg/tests/test_budget.py",
    "pkg/tests/budget_test.py",
    "pkg/tests/conftest.py",
    "ui/view.test.tsx",
    "ui/view.spec.ts",
    "ui/view-test-mock.tsx",
    "ui/view-test-mock.ts",
    "ui/test-support/render.tsx",
    "ui/lib/timeline/tests/use-timeline-compact.ts",
]
REFUSED = [
    "pkg/tests/helpers.py",
    "pkg/util.py",
    "ui/view.tsx",
    "ui/page-mock.tsx",
    "ui/page-fixtures-mock.tsx",
    "ui/page-fake.ts",
    "ui/page-double.tsx",
    "ui/test-mock-utils.ts",
    "ui/mock.tsx",
]


@pytest.mark.parametrize("path", ACCEPTED)
def test_a_test_module_is_named_the_way_its_language_names_tests(path: str) -> None:
    assert is_test_file(path)


@pytest.mark.parametrize("path", REFUSED)
def test_a_name_that_only_looks_like_a_test_module_is_a_shipped_module(path: str) -> None:
    assert not is_test_file(path)
