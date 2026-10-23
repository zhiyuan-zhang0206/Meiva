# meiva:note
# The skip ratchet's failures, and the rules it must not lose: a new skip is named by
# file, line and predicate; a predicate this tree never used still counts; and the number
# and its record move together or the run says so.

from __future__ import annotations

import pytest

from scripts.structure import skip_sites

_ONE_SKIP = {"tests/test_x.py": [(3, "skip('a')")]}

_DECORATED = (
    "import pytest\n\n\n"
    '@pytest.mark.skipif(sys.platform == "win32", reason="windows only")\n'
    "def test_x():\n    pass\n"
)

_NOVEL = (
    "import pytest\n\n\n"
    '@pytest.mark.skipif(_ready() and os.environ.get("TOKEN") == "1", reason="no")\n'
    "def test_y():\n    pass\n"
)

_FOUR = (
    "import pytest\n\n\n"
    "@pytest.mark.skipif(True, reason='a')\n"
    "def test_a():\n    pass\n\n\n"
    "@pytest.mark.skip(reason='b')\n"
    "def test_b():\n    pass\n\n\n"
    "def test_c():\n    pytest.skip('c')\n\n\n"
    "def test_d():\n    pytest.importorskip('d')\n"
)


def test_the_test_tree_holds_the_frozen_number_of_skip_sites() -> None:
    found = skip_sites.sites_by_file(skip_sites.tracked_test_modules())
    report = skip_sites.judge(
        found, allowed=skip_sites.TEST_SKIP_SITES_ALLOWED, baseline=skip_sites.load()
    )
    # meiva:note
    # The report is the assertion message, not a repr of it: a reader meets the sentence
    # "skip sites 86 > allowed 85" and the site, not a truncated dictionary.
    if report is not None:
        pytest.fail(report)


def test_the_number_is_the_sum_of_the_recorded_sites() -> None:
    assert sum(len(whats) for whats in skip_sites.load().values()) == (
        skip_sites.TEST_SKIP_SITES_ALLOWED
    )


def test_an_added_skip_fails_with_its_file_line_and_predicate() -> None:
    found = {"tests/test_x.py": skip_sites.site_strings(_DECORATED)}
    report = skip_sites.judge(found, allowed=0, baseline={})
    assert report is not None
    assert "tests: skip sites 1 > allowed 0" in report
    assert "+tests/test_x.py:4  skipif(" in report
    assert "win32" in report


def test_a_predicate_this_tree_never_used_still_counts() -> None:
    # meiva:note
    # The gate counts sites, so no vocabulary can be complete and no predicate escapes it.
    assert [what for _, what in skip_sites.site_strings(_NOVEL)] == [
        "skipif(_ready() and os.environ.get('TOKEN') == '1')"
    ]


def test_the_four_spellings_are_one_site_each_and_a_decorator_is_not_counted_twice() -> None:
    assert [what for _, what in skip_sites.site_strings(_FOUR)] == [
        "skipif(True)",
        "skip('b')",
        "skip('c')",
        "importorskip('d')",
    ]


def test_a_removed_skip_asks_for_the_lower_number() -> None:
    report = skip_sites.judge(
        {}, allowed=2, baseline={"tests/test_x.py": ("skip('a')", "skip('b')")}
    )
    assert report is not None
    assert "tests: skip sites 0 < allowed 2" in report
    assert "-1 tests/test_x.py  skip('a')" in report


def test_a_skip_that_replaces_another_fails_although_the_count_matches() -> None:
    report = skip_sites.judge(
        {"tests/test_x.py": [(3, "skip('new')")]},
        allowed=1,
        baseline={"tests/test_x.py": ("skip('old')",)},
    )
    assert report is not None
    assert "not the recorded ones" in report
    assert "+tests/test_x.py:3  skip('new')" in report
    assert "-1 tests/test_x.py  skip('old')" in report


def test_a_stale_record_is_named_too() -> None:
    # meiva:note
    # The third direction: the tree and the number agree, but the record never learned
    # about one site. A count alone cannot say which; the record's gaps can.
    report = skip_sites.judge(_ONE_SKIP, allowed=1, baseline={})
    assert report is not None
    assert "the record is missing a site" in report
    assert "+tests/test_x.py:3  skip('a')" in report


def test_lowering_the_number_without_moving_the_tree_says_so() -> None:
    # meiva:note
    # The number and the record are the same fact; editing one is not a skip change, and
    # the report has to say that rather than name a site that does not exist.
    report = skip_sites.judge(_ONE_SKIP, allowed=0, baseline={"tests/test_x.py": ("skip('a')",)})
    assert report is not None
    assert "tests: skip sites 1 > allowed 0" in report
    assert "but the tree did not move" in report
