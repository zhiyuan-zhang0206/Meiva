from __future__ import annotations

import pytest

from scripts.lint.diagnostics.diff_hints.model import FileChange, StatId
from scripts.lint.diagnostics.diff_hints.stats import statistics
from scripts.lint.diagnostics.diff_hints.tests.support import ROLES


def counted(source: str, path: str = "cli/x.py") -> list[tuple[StatId, int]]:
    text = source.lstrip("\n")
    change = FileChange(
        path, frozenset(range(1, len(text.splitlines()) + 1)), (), (), deleted=False
    )
    return [(site.stat, site.line) for site in statistics((change,), {path: text}, ROLES)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def f(client: Client | None, size: int) -> None:\n    if client is None:\n        client = Client()\n",
            [(StatId.OPTIONAL_FALLBACK, 3)],
        ),
        (
            "def f(client: Optional[Client]) -> None:\n    client = client or Client()\n",
            [(StatId.OPTIONAL_FALLBACK, 2)],
        ),
        ("def f(client: Client | None = None) -> None:\n    return None\n", []),
        ("def f(client: Client | None) -> None:\n    if client is None:\n        return\n", []),
        ("def f(size: int | None) -> int:\n    return size or 0\n", []),
    ],
)
def test_a_required_optional_parameter_that_falls_back_is_counted(
    source: str, expected: list[tuple[StatId, int]]
) -> None:
    only = [(stat, line) for stat, line in counted(source) if stat is StatId.OPTIONAL_FALLBACK]
    assert only == expected


def test_a_parameter_with_a_default_is_a_candidate_and_a_receiver_is_not() -> None:
    source = "class A:\n    def go(self, size: int = 3, *, name: str = 'x') -> None: ...\n"
    found = counted(source)
    assert found == [(StatId.OPTIONAL_TEST_ONLY, 2), (StatId.OPTIONAL_TEST_ONLY, 2)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from base.clock import Clock\n\ndef run(config):\n    return Clock(config)\n",
            [(StatId.CLOCK_IN_CALLEE, 4)],
        ),
        (
            "from base.clock import Clock\n\ndef run(config):\n    return Clock.from_settings()\n",
            [(StatId.CLOCK_IN_CALLEE, 4)],
        ),
        ("from base.clock import Clock\n\ndef run():\n    return Clock.from_settings()\n", []),
        ("from other import Clock\n\ndef run(config):\n    return Clock(config)\n", []),
    ],
)
def test_a_clock_built_in_a_function_that_takes_parameters_is_counted(
    source: str, expected: list[tuple[StatId, int]]
) -> None:
    only = [(stat, line) for stat, line in counted(source) if stat is StatId.CLOCK_IN_CALLEE]
    assert only == expected
