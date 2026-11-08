# meiva:note
# The decision `e2e gate` makes, and the two things it must never do silently: red that `main`
# already has must not block a pull request, and red the pull request added must. Each test is one
# half of that pair, plus the ways the authoritative set can go missing -- the nightly reads empty,
# is stale, or describes another base -- because every one of those has to fail instead of passing
# on an empty set.

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from schedules.jobs.watch import inherited_reds as reds

_REPOSITORY = "acme/meiva"
_BRANCH = "main"
_RUN_ID = 4242
_ATTEMPT = 2
_NIGHTLY_RUN_ID = 3131
_NIGHTLY_SHA = "a" * 40
_BASE_SHA = "b" * 40
_NODEID = (
    "tests/e2e/state/test_timeline_virtualization.py"
    "::test_parked_deep_history_mounts_a_bounded_window[agent_chat]"
)
_OTHER_NODEID = "tests/e2e/test_browser.py::test_smoke"
_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
# meiva:note
# What the gate appends to a paged endpoint: the first page of a hundred, which is every page these
# tables carry.
_PAGE = "?per_page=100&page=1"


# meiva:note
# The URL a table entry names: the same path without the pagination parameters GitHub's paged reader
# appends, so one entry answers every page request of that endpoint.
def _page_key(url: str) -> str:
    path, _, query = url.partition("?")
    kept = [
        part for part in query.split("&") if part and not part.startswith(("per_page=", "page="))
    ]
    return f"{path}?{'&'.join(kept)}" if kept else path


# meiva:note
# A `gh` that answers this test's documents, keyed by the API path it was asked for, with pagination
# parameters ignored unless the test names the paged URL itself -- which is how one test drives real
# paging. Bytes are a download and anything else is a JSON answer, so one table serves `api` and
# `json`; a path the table does not carry, or a non-zero `returncode`, reaches the real failures.
def _gh(
    answers: Mapping[str, object],
    *,
    returncode: int = 0,
    stderr: bytes = b"",
    asked: list[str] | None = None,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        url = argv[2]
        if asked is not None:
            asked.append(url)
        payload = answers.get(url, answers.get(_page_key(url)))
        stdout = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return subprocess.CompletedProcess(
            args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


# meiva:note
# The envelope each paged endpoint answers with: the gate reads rows out of the key GitHub names.
def _jobs(*rows: dict[str, object]) -> dict[str, object]:
    return {"jobs": list(rows)}


def _artifacts(*rows: dict[str, object]) -> dict[str, object]:
    return {"artifacts": list(rows)}


def _runs(*rows: dict[str, object]) -> dict[str, object]:
    return {"workflow_runs": list(rows)}


# meiva:note
# The gate's GitHub as the workflow builds it, with the `gh` above underneath.
def _api(
    answers: Mapping[str, object],
    *,
    returncode: int = 0,
    stderr: bytes = b"",
    asked: list[str] | None = None,
) -> reds.GitHub:
    return reds.GitHub(_gh(answers, returncode=returncode, stderr=stderr, asked=asked))


def _jobs_path(run_id: int = _RUN_ID, attempt: int = _ATTEMPT) -> str:
    return f"repos/{_REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs"


def _artifacts_path(run_id: int = _RUN_ID) -> str:
    return f"repos/{_REPOSITORY}/actions/runs/{run_id}/artifacts"


def _zip_path(artifact_id: int) -> str:
    return f"repos/{_REPOSITORY}/actions/artifacts/{artifact_id}/zip"


_NIGHTLY_PATH = f"repos/{_REPOSITORY}/actions/workflows/ci.yml/runs?branch={_BRANCH}&event=schedule"
_COMPARE_PATH = f"repos/{_REPOSITORY}/compare/{_NIGHTLY_SHA}...{_BASE_SHA}"


def _job(name: str, conclusion: str, started: str, completed: str) -> dict[str, object]:
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
    }


def _artifact(artifact_id: int, name: str, created: str) -> dict[str, object]:
    return {"id": artifact_id, "name": name, "created_at": created}


def _nightly_run(
    *,
    run_id: int = _NIGHTLY_RUN_ID,
    attempt: int = 1,
    sha: str = _NIGHTLY_SHA,
    completed: str = "2026-11-08T07:00:00Z",
    conclusion: str = "failure",
    status: str = "completed",
) -> dict[str, object]:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "head_sha": sha,
        "run_attempt": attempt,
        "updated_at": completed,
    }


# meiva:note
# One JUnit document in a zip: `(file, test name, failed)` per case.
def _junit_zip(cases: Sequence[tuple[str, str, bool]]) -> bytes:
    body = "".join(
        f'<testcase file="{file}" name="{name}">{"<failure/>" if failed else ""}</testcase>'
        for file, name, failed in cases
    )
    document = f"<testsuite>{body}</testsuite>".encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("junit-e2e-shard-3.xml", document)
    return buffer.getvalue()


# meiva:note
# The TOML a pull request adds: one `[[inherited_red]]` per known red.
def _declaration(*entries: tuple[str, str, date]) -> str:
    lines: list[str] = []
    for nodeid, card, expires in entries:
        lines.append("[[inherited_red]]")
        lines.append(f'nodeid = "{nodeid}"')
        lines.append(f'card = "{card}"')
        lines.append(f"expires = {expires.isoformat()}")
    if not entries:
        lines.append("inherited_red = []")
    return "\n".join(lines) + "\n"


def _red(
    nodeid: str = _NODEID, *, card: str = "#6475", expires: date = date(2026, 11, 15)
) -> reds.DeclaredRed:
    return reds.DeclaredRed(nodeid=nodeid, card=card, expires=expires)


def _nightly(
    *,
    completed_at: datetime = datetime(2026, 11, 8, 7, 0, tzinfo=UTC),
    conclusion: str = "failure",
) -> reds.Nightly:
    return reds.Nightly(
        run_id=_NIGHTLY_RUN_ID,
        head_sha=_NIGHTLY_SHA,
        attempt=1,
        completed_at=completed_at,
        conclusion=conclusion,
    )


# meiva:note
# The decision: `B` (what this pull request saw) against `A` (the nightly's reds, declared and
# unexpired).
def test_a_red_the_nightly_already_had_passes_and_the_summary_names_its_owner() -> None:
    verdict = reds.judge(
        observed=frozenset({_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(_red(),)),
        nightly=_nightly(),
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert verdict.passed, verdict.reason
    summary = verdict.summary(now=_NOW)
    assert f"run {_NIGHTLY_RUN_ID} attempt 1 sha {_NIGHTLY_SHA[:12]}" in summary
    assert "completed 2026-11-08T07:00:00+00:00 (5.0h ago)" in summary
    assert "completed 2026-11-08T07:00:00+00:00 (5.0h ago)" in summary
    assert "nightly head is an ancestor of this base: yes" in summary
    assert "inherited red 1:" in summary
    assert f"  - {_NODEID} (card #6475, expires 2026-11-15)" in summary
    assert "new red" not in summary


def test_a_red_the_nightly_did_not_have_blocks_and_is_named() -> None:
    verdict = reds.judge(
        observed=frozenset({_NODEID, _OTHER_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(_red(), _red(_OTHER_NODEID))),
        nightly=_nightly(),
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert not verdict.passed
    assert verdict.fresh == (_OTHER_NODEID,)
    assert verdict.inherited == (_red(),)
    summary = verdict.summary(now=_NOW)
    assert "new red 1:" in summary
    assert f"  - {_OTHER_NODEID}" in summary


def test_a_nightly_red_nobody_declared_still_blocks() -> None:
    # meiva:note
    # The declared set is a person signing for a red; the nightly running red is not enough.
    verdict = reds.judge(
        observed=frozenset({_OTHER_NODEID}),
        nightly_failures=frozenset({_OTHER_NODEID}),
        declaration=reds.Declaration(reds=()),
        nightly=_nightly(),
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert not verdict.passed
    assert verdict.fresh == (_OTHER_NODEID,)


def test_an_expired_entry_excuses_nothing() -> None:
    expired = _red(expires=date(2026, 11, 7))
    verdict = reds.judge(
        observed=frozenset({_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(expired,)),
        nightly=_nightly(),
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert not verdict.passed
    assert verdict.inherited == ()
    assert verdict.fresh == (_NODEID,)


def test_an_entry_expires_at_the_end_of_its_day() -> None:
    verdict = reds.judge(
        observed=frozenset({_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(_red(expires=_NOW.date()),)),
        nightly=_nightly(),
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert verdict.passed


def test_a_base_that_does_not_stand_on_the_nightly_head_blocks() -> None:
    verdict = reds.judge(
        observed=frozenset({_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(_red(),)),
        nightly=_nightly(),
        nightly_is_ancestor=False,
        now=_NOW,
    )
    assert not verdict.passed
    assert "is not an ancestor of the pull request base" in verdict.reason
    assert verdict.summary(now=_NOW).endswith("inherited red 0:")


def test_a_stale_nightly_blocks() -> None:
    stale = _nightly(completed_at=_NOW - reds.MAX_NIGHTLY_AGE - timedelta(minutes=1))
    verdict = reds.judge(
        observed=frozenset({_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(_red(),)),
        nightly=stale,
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert not verdict.passed
    assert "old" in verdict.reason


def test_the_oldest_nightly_that_is_young_enough_still_answers() -> None:
    verdict = reds.judge(
        observed=frozenset({_NODEID}),
        nightly_failures=frozenset({_NODEID}),
        declaration=reds.Declaration(reds=(_red(),)),
        nightly=_nightly(completed_at=_NOW - reds.MAX_NIGHTLY_AGE),
        nightly_is_ancestor=True,
        now=_NOW,
    )
    assert verdict.passed


# meiva:note
# Reading the nightly: the set is the run's final attempt, never an attempt it replaced.
def test_the_nightly_is_read_at_its_final_attempt() -> None:
    api = _api(
        {
            _NIGHTLY_PATH: _runs(
                _nightly_run(status="in_progress", conclusion="", completed="2026-11-08T09:00:00Z"),
                _nightly_run(attempt=2),
            )
        }
    )
    nightly = reds.completed_nightly(api, repository=_REPOSITORY, branch=_BRANCH)
    assert (nightly.run_id, nightly.attempt) == (_NIGHTLY_RUN_ID, 2)


def test_no_completed_nightly_fails_closed() -> None:
    api = _api({_NIGHTLY_PATH: _runs(_nightly_run(status="in_progress", conclusion=""))})
    with pytest.raises(reds.DeclarationError):
        reds.completed_nightly(api, repository=_REPOSITORY, branch=_BRANCH)


# meiva:note
# Anchoring: the artifact of the attempt that failed, not the newest one that shares its name.
def test_only_the_failing_job_of_this_attempt_selects_its_artifact() -> None:
    jobs = [
        _job("e2e shard (3/4)", "failure", "2026-11-08T02:00:00Z", "2026-11-08T02:20:00Z"),
        _job("e2e shard (1/4)", "success", "2026-11-08T02:00:00Z", "2026-11-08T02:20:00Z"),
    ]
    artifacts = [
        # meiva:note
        # The attempt that failed, uploaded inside its own job's window.
        _artifact(11, "test-results-e2e-shard-3", "2026-11-08T02:20:30Z"),
        # meiva:note
        # The shard that passed: no failing job names it, so its junit is never read.
        _artifact(12, "test-results-e2e-shard-1", "2026-11-08T02:20:30Z"),
        # meiva:note
        # The rerun's green attempt, the same name four hours later.
        _artifact(13, "test-results-e2e-shard-3", "2026-11-08T06:20:30Z"),
        # meiva:note
        # A log bundle, not junit.
        _artifact(14, "e2e-logs-shard-3", "2026-11-08T02:20:30Z"),
    ]
    assert reds.result_artifact_ids(artifacts, jobs) == (11,)


def test_the_attempt_reads_the_junit_of_its_own_artifact() -> None:
    asked: list[str] = []
    api = _api(
        {
            _jobs_path(): _jobs(
                _job("e2e shard (3/4)", "failure", "2026-11-08T02:00:00Z", "2026-11-08T02:20:00Z")
            ),
            _artifacts_path(): _artifacts(
                _artifact(11, "test-results-e2e-shard-3", "2026-11-08T02:20:30Z"),
                _artifact(13, "test-results-e2e-shard-3", "2026-11-08T06:20:30Z"),
            ),
            _zip_path(11): _junit_zip(
                [
                    (
                        "tests/e2e/state/test_timeline_virtualization.py",
                        "test_parked_deep_history_mounts_a_bounded_window[agent_chat]",
                        True,
                    ),
                    ("tests/e2e/test_browser.py", "test_smoke", False),
                ]
            ),
            _zip_path(13): _junit_zip([]),
        },
        asked=asked,
    )
    observed = reds.failures_of_attempt(
        api, repository=_REPOSITORY, run_id=_RUN_ID, attempt=_ATTEMPT
    )
    assert observed == frozenset({_NODEID})
    assert asked == [
        _jobs_path() + _PAGE,
        _artifacts_path() + _PAGE,
        _zip_path(11),
    ]


def test_an_artifact_that_is_not_a_zip_fails_closed() -> None:
    api = _api(
        {
            _jobs_path(): _jobs(
                _job("e2e shard (3/4)", "failure", "2026-11-08T02:00:00Z", "2026-11-08T02:20:00Z")
            ),
            _artifacts_path(): _artifacts(
                _artifact(11, "test-results-e2e-shard-3", "2026-11-08T02:20:30Z")
            ),
            _zip_path(11): b"<html>not a zip</html>",
        }
    )
    with pytest.raises(reds.DeclarationError):
        reds.failures_of_attempt(api, repository=_REPOSITORY, run_id=_RUN_ID, attempt=_ATTEMPT)


# meiva:note
# Ancestry comes from the graph, not from the clock, and the direction that is asked is the one that
# can fire: `compare(nightly_head...base)` answering `ahead` means the base contains the nightly head.
# The same answer read the other way round (`base...nightly_head` = `behind`) is the case this gate
# must reject, because a base behind the nightly lacks the commits the nightly tested.
@pytest.mark.parametrize(
    "status, expected",
    [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)],
)
def test_the_base_inherits_the_nightly_head(status: str, expected: bool) -> None:
    api = _api({_COMPARE_PATH: {"status": status}})
    assert (
        reds.nightly_is_ancestor(
            api, repository=_REPOSITORY, nightly_head=_NIGHTLY_SHA, base_sha=_BASE_SHA
        )
        is expected
    )


# meiva:note
# The declaration carries two signatures, and a set that cannot be read is not an empty set.
def test_an_empty_declaration_is_a_read_set_not_a_missing_one() -> None:
    assert reds.load_declaration(_declaration()) == reds.Declaration(reds=())


def test_an_entry_without_its_card_is_refused() -> None:
    with pytest.raises(reds.DeclarationError, match="card"):
        reds.load_declaration(f'[[inherited_red]]\nnodeid = "{_NODEID}"\nexpires = 2026-11-15\n')


def test_an_entry_without_a_date_is_refused() -> None:
    with pytest.raises(reds.DeclarationError, match="expires"):
        reds.load_declaration(f'[[inherited_red]]\nnodeid = "{_NODEID}"\ncard = "#6475"\n')
    with pytest.raises(reds.DeclarationError, match="expires"):
        reds.load_declaration(
            f'[[inherited_red]]\nnodeid = "{_NODEID}"\ncard = "#6475"\nexpires = "2026-11-15"\n'
        )


def test_an_entry_expiring_at_an_instant_is_refused() -> None:
    # meiva:note
    # A TOML datetime parses as a `date` too, and comparing it with today raises where nobody reads it.
    with pytest.raises(reds.DeclarationError, match="TOML date"):
        reds.load_declaration(
            f'[[inherited_red]]\nnodeid = "{_NODEID}"\ncard = "#6475"\nexpires = 2026-11-15T00:00:00\n'
        )


def test_one_red_declared_twice_is_refused() -> None:
    with pytest.raises(reds.DeclarationError, match="repeats"):
        reds.load_declaration(
            _declaration(
                (_NODEID, "#6475", date(2026, 11, 15)), (_NODEID, "#6476", date(2026, 11, 15))
            )
        )


def test_a_nodeid_without_a_test_name_is_refused() -> None:
    with pytest.raises(reds.DeclarationError, match="nodeid"):
        reds.load_declaration(_declaration(("just_a_name", "#6475", date(2026, 11, 15))))


# meiva:note
# The entry point: what the workflow runs, and what it prints.
def _argv(*, suite: str = "failure", base: str = _BASE_SHA) -> list[str]:
    return [
        "--classify",
        "success",
        "--suite",
        suite,
        "--base-sha",
        base,
        "--repository",
        _REPOSITORY,
        "--run-id",
        str(_RUN_ID),
        "--run-attempt",
        str(_ATTEMPT),
        "--branch",
        _BRANCH,
    ]


def _happy_answers() -> dict[str, object]:
    return {
        _jobs_path(): _jobs(
            _job("e2e shard (3/4)", "failure", "2026-11-08T02:00:00Z", "2026-11-08T02:20:00Z")
        ),
        _artifacts_path(): _artifacts(
            _artifact(11, "test-results-e2e-shard-3", "2026-11-08T02:20:30Z")
        ),
        _zip_path(11): _junit_zip(
            [
                (
                    "tests/e2e/state/test_timeline_virtualization.py",
                    "test_parked_deep_history_mounts_a_bounded_window[agent_chat]",
                    True,
                )
            ]
        ),
        _NIGHTLY_PATH: _runs(_nightly_run()),
        _jobs_path(_NIGHTLY_RUN_ID, 1): _jobs(
            _job("e2e shard (3/4)", "failure", "2026-11-08T06:00:00Z", "2026-11-08T06:20:00Z")
        ),
        _artifacts_path(_NIGHTLY_RUN_ID): _artifacts(
            _artifact(21, "test-results-e2e-shard-3", "2026-11-08T06:20:30Z")
        ),
        _zip_path(21): _junit_zip(
            [
                (
                    "tests/e2e/state/test_timeline_virtualization.py",
                    "test_parked_deep_history_mounts_a_bounded_window[agent_chat]",
                    True,
                )
            ]
        ),
        _COMPARE_PATH: {"status": "ahead"},
    }


def test_the_gate_passes_and_prints_every_inherited_red(capsys: pytest.CaptureFixture[str]) -> None:
    api = _api(_happy_answers())
    code = reds.main(
        _argv(),
        environ={"GITHUB_REPOSITORY": _REPOSITORY},
        now=_NOW,
        api=api,
        declaration_text=_declaration((_NODEID, "#6475", date(2026, 11, 15))),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "e2e gate: PASS" in out
    assert f"inherited red 1:\n  - {_NODEID} (card #6475, expires 2026-11-15)" in out


def test_the_gate_blocks_a_red_the_pull_request_added(capsys: pytest.CaptureFixture[str]) -> None:
    answers = _happy_answers()
    observed_zip = _junit_zip(
        [
            (
                "tests/e2e/state/test_timeline_virtualization.py",
                "test_parked_deep_history_mounts_a_bounded_window[agent_chat]",
                True,
            ),
            ("tests/e2e/test_browser.py", "test_smoke", True),
        ]
    )
    answers[_zip_path(11)] = observed_zip
    code = reds.main(
        _argv(),
        environ={"GITHUB_REPOSITORY": _REPOSITORY},
        now=_NOW,
        api=_api(answers),
        declaration_text=_declaration((_NODEID, "#6475", date(2026, 11, 15))),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "e2e gate: FAIL" in out
    assert "new red 1:" in out
    assert "  - tests/e2e/test_browser.py::test_smoke" in out


def test_a_suite_that_succeeded_never_reads_github(capsys: pytest.CaptureFixture[str]) -> None:
    asked: list[str] = []
    api = _api({}, asked=asked)
    assert reds.main(_argv(suite="success"), environ={}, now=_NOW, api=api) == 0
    assert asked == []
    assert "e2e gate: PASS - the suite is success" in capsys.readouterr().out


def test_classification_failure_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--classify", "failure", "--suite", "success"]
    assert reds.main(argv, environ={}, now=_NOW, api=_api({})) == 1
    assert "e2e gate: FAIL - classify is failure" in capsys.readouterr().out


def test_a_failed_suite_without_a_base_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    assert reds.main(_argv(base=""), environ={}, now=_NOW, api=_api({})) == 1
    assert "no pull request base" in capsys.readouterr().out


def test_a_failed_suite_with_no_readable_failure_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    # meiva:note
    # Nothing failed in the junit does not mean nothing failed: it means the evidence is missing.
    answers = _happy_answers()
    answers[_zip_path(11)] = _junit_zip([])
    code = reds.main(
        _argv(), environ={}, now=_NOW, api=_api(answers), declaration_text=_declaration()
    )
    assert code == 1
    assert "no failing test identity could be read" in capsys.readouterr().out


def test_an_unreadable_authoritative_set_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    answers = _happy_answers()
    del answers[_NIGHTLY_PATH]
    code = reds.main(
        _argv(),
        environ={},
        now=_NOW,
        api=_api(answers),
        declaration_text=_declaration((_NODEID, "#6475", date(2026, 11, 15))),
    )
    assert code == 1
    assert "no authoritative red set" in capsys.readouterr().out


def test_a_declaration_that_cannot_be_parsed_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    code = reds.main(
        _argv(),
        environ={},
        now=_NOW,
        api=_api(_happy_answers()),
        declaration_text="not toml [[[",
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "no authoritative red set" in out
    assert "not valid TOML" in out


def test_a_declaration_file_that_is_not_there_blocks(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    argv = [*_argv(), "--declaration", str(tmp_path / "missing.toml")]
    assert reds.main(argv, environ={}, now=_NOW, api=_api(_happy_answers())) == 1
    out = capsys.readouterr().out
    assert "no authoritative red set" in out
    assert "could not be read" in out


# meiva:note
# Paging is the one place the query has to be part of the key, so this table names both pages.
def test_paged_follows_every_page() -> None:
    answers = {
        "repos/x/y/runs?per_page=100&page=1": {"jobs": [{"id": index} for index in range(100)]},
        "repos/x/y/runs?per_page=100&page=2": {"jobs": [{"id": 100}]},
    }
    rows = reds.GitHub(_gh(answers)).paged("repos/x/y/runs", "jobs")
    assert len(rows) == 101


def test_a_failed_gh_call_names_the_command() -> None:
    run = _gh({}, returncode=1, stderr=b"gh: Not Found (HTTP 404)")
    with pytest.raises(reds.DeclarationError, match="HTTP 404"):
        reds.GitHub(run).json("repos/x/y/nothing")
