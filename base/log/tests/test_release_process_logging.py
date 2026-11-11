from __future__ import annotations

from pathlib import Path

import pytest

from base.log.process import release_process_logging, start_process_logging


def test_released_process_logging_can_start_again_and_a_second_release_is_a_no_op(
    tmp_path: Path,
) -> None:
    try:
        assert start_process_logging("probe", attach_events=lambda: None, home=tmp_path) is not None
        assert start_process_logging("probe", attach_events=lambda: None, home=tmp_path) is None

        release_process_logging()

        assert start_process_logging("probe", attach_events=lambda: None, home=tmp_path) is not None
    finally:
        release_process_logging()
    release_process_logging()


_CONFTEST = """
pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.plugin_registrations",
    "tests.fixtures.guards",
]
"""

_CASES = """
def test_a_opens_the_process_sinks_and_never_closes_them(tmp_path):
    from base.log import process

    assert process.start_process_logging("probe", attach_events=lambda: None, home=tmp_path)
    assert process.init_done


def test_b_starts_in_a_process_the_test_before_it_left_clean(tmp_path):
    from base.log import process

    assert not process.init_done
    assert process.start_process_logging("probe", attach_events=lambda: None, home=tmp_path)
"""


def test_the_suite_releases_sinks_a_test_leaves_open_so_the_next_test_starts_clean(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_CASES)

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=2)
