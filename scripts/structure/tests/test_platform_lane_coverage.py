# meiva:note
# The lane gate's failures, one case each, and the tree it judges. The cases exist because
# each one is a way the promise "a CI lane runs it there" can be true on paper and false in
# practice: a lane whose job does not exist, a job that runs on the other host, a job whose
# verdict is informational, a declaration a marked lane cannot select, and a census of zero
# that looks exactly like a green tree.

from __future__ import annotations

import pytest

from scripts.structure import platform_lane_coverage as coverage
from scripts.structure.platform_lane_coverage import Job, Lane, Site


def _site(
    *,
    system: str = "darwin",
    path: str = "tests/test_x.py",
    test: str = "test_x",
    declared: str = "marker",
) -> Site:
    return Site(system=system, path=path, test=test, declared=declared)


def _lane(*, host: str = "darwin", job: str = "mac", selects: str = "marked") -> Lane:
    return Lane(id=f"{host}-{selects}", host=host, job=job, selects=selects)


def _jobs(
    *, runner: str = "macos-15", system: str | None = "darwin", blocking: bool = True
) -> dict[str, Job]:
    return {"mac": Job(id="mac", runs_on=runner, system=system, blocking=blocking)}


# meiva:note
# The gate CI runs. Its report is the failure message: a reader meets the sentence naming the
# declaration and the reason it is unrun, not a truncated dictionary.
def test_the_tree_has_a_lane_for_every_host_it_declares() -> None:
    report = coverage.judge(
        coverage.all_sites(), coverage.lanes(), coverage.jobs(), coverage.UNRUN_ALLOWED
    )
    if report is not None:
        pytest.fail(report)


def test_the_census_has_a_positive_denominator() -> None:
    # meiva:note
    # The gate refuses an empty census, and this says so twice on purpose: a tree whose
    # declarations were deleted must not read as covered.
    assert coverage.all_sites()


def test_a_declaration_its_lane_selects_passes() -> None:
    assert coverage.judge([_site()], [_lane()], _jobs(), ()) is None


def test_an_empty_census_fails() -> None:
    report = coverage.judge([], [], {}, ())
    assert report is not None
    assert "census of zero proves nothing" in report


def test_a_marked_lane_covers_only_the_marker() -> None:
    report = coverage.judge([_site(declared="skip")], [_lane()], _jobs(), ())
    assert report is not None
    assert "undeclared tests/test_x.py::test_x" in report
    assert '@pytest.mark.requires_host("darwin")' in report


def test_a_suite_lane_covers_the_host_it_runs_on() -> None:
    site = _site(system="linux", declared="marker")
    lane = _lane(host="linux", job="shard", selects="suite")
    jobs = {"shard": Job(id="shard", runs_on="ubuntu-24.04", system="linux", blocking=True)}
    assert coverage.judge([site], [lane], jobs, ()) is None


def test_a_lane_naming_a_missing_job_is_named() -> None:
    report = coverage.judge([_site()], [_lane(job="dos")], _jobs(), ())
    assert report is not None
    assert "names job 'dos'" in report


def test_a_job_on_another_host_is_named() -> None:
    report = coverage.judge([_site()], [_lane()], _jobs(runner="ubuntu-24.04", system="linux"), ())
    assert report is not None
    assert "declares host darwin but job 'mac' runs on ubuntu-24.04" in report


def test_a_runner_whose_host_cannot_be_read_is_named() -> None:
    report = coverage.judge([_site()], [_lane()], _jobs(system=None), ())
    assert report is not None
    assert "the host it tests on cannot be read" in report


def test_a_decorative_job_covers_nothing() -> None:
    # meiva:note
    # The lane exists, runs on the right host, selects the marker - and judges nothing. Both
    # halves of the answer have to appear: the job is decoration, and the declaration is
    # therefore unrun.
    report = coverage.judge([_site()], [_lane()], _jobs(blocking=False), ())
    assert report is not None
    assert "carries continue-on-error" in report
    assert "+tests/test_x.py::test_x" in report


def test_an_unrecorded_gap_is_named_with_its_site() -> None:
    report = coverage.judge([_site()], [], {}, ())
    assert report is not None
    assert "+tests/test_x.py::test_x" in report


def test_a_recorded_gap_that_is_covered_now_is_named() -> None:
    report = coverage.judge([_site()], [_lane()], _jobs(), ("tests/test_x.py::test_x",))
    assert report is not None
    assert "-tests/test_x.py::test_x" in report


def test_the_marker_is_read_from_a_decorator_a_class_and_a_module_pytestmark() -> None:
    source = (
        "import pytest\n\n"
        "pytestmark = pytest.mark.requires_host('linux')\n\n\n"
        "@pytest.mark.requires_host('darwin')\n"
        "class TestX:\n"
        "    @pytest.mark.requires_host('darwin')\n"
        "    def test_method(self):\n"
        "        pass\n"
    )
    sites = coverage.sites_in_source("tests/test_x.py", source)
    assert [(site.system, site.test) for site in sites] == [
        ("linux", "<module>"),
        ("darwin", "TestX"),
        ("darwin", "TestX::test_method"),
    ]


def test_the_other_polarity_is_not_a_declaration() -> None:
    # meiva:note
    # `skipif(sys.platform == "darwin")` skips ON the host, so it needs no lane on macOS; a
    # conjunction is not one system either. Reading them as declarations would invent demand.
    source = (
        "import pytest\nimport sys\n\n"
        "@pytest.mark.skipif(sys.platform == 'darwin', reason='not on mac')\n"
        "def test_a():\n    pass\n\n"
        "@pytest.mark.skipif(sys.platform != 'darwin' and True, reason='two')\n"
        "def test_b():\n    pass\n"
    )
    assert coverage.sites_in_source("tests/test_x.py", source) == []
