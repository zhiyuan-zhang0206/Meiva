# meiva:note
# The main-red watch's decisions, replayed against the two real failures of 2026-11-01: one that
# the nightly passed on the same commit an hour later, and one that no run explained.
# meiva:link schedules/README.md

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schedules.plans.watch.main_red import (
    FAILURE_INFRA,
    GATE_CHECK_STEPS,
    FAILURE_TEST,
    NIGHTLY_STALE_AFTER,
    RELEASE_GATE_STALE_AFTER,
    STATE_CONFIRMED,
    STATE_SUSPECTED,
    Entry,
    Failure,
    RunFacts,
    apply_run,
    card_body,
    card_title,
    fingerprint,
    liveness_alert,
    parse_failures,
)

_GATE = "release-gate.yml"
_NIGHTLY = "refresh-test-durations.yml"
_SHA = "b32d5a8662"
_SEMAPHORE = (
    "services/agent_runner/agent_ops/tests/daemon/test_ops_route.py::"
    "test_the_semaphore_caps_how_many_requests_dispatch_at_once[2-True]"
)
_WATCH_WORK = (
    "meiva_builtins.skills.platform.meiva-guide.external-agents.tests.test_watch_work::"
    "test_a_stalled_canonical_run_wakes_the_owner_at_each_heartbeat"
)


def _red(workflow: str, sha: str, when: str, *test_ids: str) -> RunFacts:
    return RunFacts(
        workflow=workflow,
        head_sha=sha,
        created_at=datetime.fromisoformat(when),
        conclusion="failure",
        failures=tuple(Failure(test_id, FAILURE_TEST) for test_id in test_ids),
    )


def _green(workflow: str, sha: str, when: str) -> RunFacts:
    return RunFacts(
        workflow=workflow,
        head_sha=sha,
        created_at=datetime.fromisoformat(when),
        conclusion="success",
        failures=(),
    )


def test_a_red_the_same_commit_explains_never_becomes_a_card() -> None:
    first = apply_run({}, _red(_GATE, _SHA, "2026-11-01T22:05:00+00:00", _SEMAPHORE))
    assert [entry.state for entry in dict(first.entries).values()] == [STATE_SUSPECTED]
    assert first.confirmed == ()

    second = apply_run(dict(first.entries), _green(_NIGHTLY, _SHA, "2026-11-01T22:57:00+00:00"))

    assert dict(second.entries) == {}
    assert second.confirmed == ()
    assert second.closes == ()


def test_a_red_no_run_explains_waits_for_a_second_commit() -> None:
    first = apply_run({}, _red(_GATE, "d080085c74", "2026-11-01T17:56:00+00:00", _WATCH_WORK))

    assert dict(first.entries)[fingerprint(_GATE, _WATCH_WORK)].state == STATE_SUSPECTED
    assert first.confirmed == ()
    assert first.closes == ()


def test_the_same_red_on_another_commit_confirms_exactly_once() -> None:
    first = apply_run({}, _red(_GATE, "aaa111", "2026-11-02T00:00:00+00:00", _WATCH_WORK))
    second = apply_run(
        dict(first.entries), _red(_GATE, "bbb222", "2026-11-02T06:00:00+00:00", _WATCH_WORK)
    )
    third = apply_run(
        dict(second.entries), _red(_GATE, "ccc333", "2026-11-02T12:00:00+00:00", _WATCH_WORK)
    )

    assert len(second.confirmed) == 1
    assert third.confirmed == ()
    entry = dict(third.entries)[fingerprint(_GATE, _WATCH_WORK)]
    assert entry.state == STATE_CONFIRMED
    assert entry.red_count == 3


def test_four_red_slots_produce_one_card_and_a_count_of_four() -> None:
    entries: dict[str, Entry] = {}
    confirmed = 0
    for index in range(4):
        plan = apply_run(
            entries,
            _red(_GATE, f"sha{index}", f"2026-11-02T{index * 6:02d}:00:00+00:00", _WATCH_WORK),
        )
        entries = dict(plan.entries)
        confirmed += len(plan.confirmed)

    (entry,) = dict(entries).values()
    assert confirmed == 1
    assert entry.red_count == 4


def test_an_infrastructure_failure_is_recorded_and_never_confirmed() -> None:
    failures = parse_failures(
        "Error response from daemon: No space left on device",
        job_name="Backend",
        suite_step_failed=True,
    )
    assert [failure.kind for failure in failures] == [FAILURE_INFRA]

    plan = apply_run(
        {}, RunFacts(_GATE, "sha1", datetime(2026, 11, 2, tzinfo=UTC), "failure", failures)
    )

    assert plan.entries == ()
    assert plan.confirmed == ()
    assert any("infra" in note for note in plan.notes)


def test_a_red_naming_nothing_is_not_a_failure_it_can_confirm() -> None:
    plan = apply_run({}, RunFacts(_GATE, "sha1", datetime(2026, 11, 2, tzinfo=UTC), "failure", ()))

    assert plan.entries == ()
    assert plan.confirmed == ()
    assert any("named no failure" in note for note in plan.notes)


def test_both_failure_line_shapes_are_read() -> None:
    pytest_shape = "FAILED tests/x/test_y.py::test_z - AssertionError"
    native_shape = "  FAILED package.module::test_z"

    assert parse_failures(pytest_shape, job_name="j", suite_step_failed=True)[0].test_id == (
        "tests/x/test_y.py::test_z"
    )
    assert parse_failures(native_shape, job_name="j", suite_step_failed=True)[0].test_id == (
        "package.module::test_z"
    )


def test_a_green_run_does_not_resolve_a_later_red() -> None:
    red = apply_run({}, _red(_GATE, "new", "2026-11-02T06:00:00+00:00", _WATCH_WORK))
    plan = apply_run(dict(red.entries), _green(_GATE, "old", "2026-11-02T00:00:00+00:00"))

    assert plan.entries != {}


def test_the_card_carries_the_nine_fields() -> None:
    plan = apply_run({}, _red(_GATE, "aaa111", "2026-11-02T00:00:00+00:00", _WATCH_WORK))
    (entry,) = dict(plan.entries).values()

    title = card_title(entry)
    body = card_body(
        entry,
        latest_url="https://example/run/1",
        job_names=("Backend full suite (4/8)",),
        candidate_commits=("abc123 landed the change",),
    )

    assert title.startswith("main-red: ")
    for field in (
        "Fingerprint",
        "Evidence",
        "First seen",
        "Latest red",
        "Red runs",
        "Last green",
        "Candidate commits",
        "Class",
        "Closes when",
    ):
        assert field in body


def test_the_schedule_alert_names_the_schedule_not_the_code() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    quiet = liveness_alert(
        "release-gate", now - timedelta(hours=5), now, stale_after=RELEASE_GATE_STALE_AFTER
    )
    stale = liveness_alert(
        "release-gate", now - timedelta(hours=12), now, stale_after=RELEASE_GATE_STALE_AFTER
    )
    missing = liveness_alert("nightly", None, now, stale_after=NIGHTLY_STALE_AFTER)

    assert quiet is None
    assert stale is not None and "the schedule, not the code" in stale
    assert missing is not None


# meiva:note
# A stream nobody has seen run yet is not a stopped schedule: a newly added nightly has no run on
# record until its first cron comes round, and one threshold has to pass before that silence says
# anything. A stream older than the watch passes no `since` and keeps reporting at once.
def test_a_stream_not_yet_observed_for_a_threshold_is_not_reported() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    since = now - timedelta(hours=1)

    assert (
        liveness_alert("nightly-full", None, now, stale_after=NIGHTLY_STALE_AFTER, since=since)
        is None
    )
    overdue = liveness_alert(
        "nightly-full",
        None,
        now + NIGHTLY_STALE_AFTER,
        stale_after=NIGHTLY_STALE_AFTER,
        since=since,
    )
    assert overdue is not None and "never started" in overdue
    assert since.isoformat() in overdue


# meiva:note
# The push gate's checks print no JUnit line, so a failed check step is the only thing that can name
# its red. Without this a `meizel check` violation would read as an unnamed failure — infra — and
# open no card, which is the case the gate exists for.
def test_a_failed_gate_check_step_names_the_red_instead_of_going_infra() -> None:
    named = parse_failures(
        "meizel: 1 violations [unresolved-import: 1]",
        job_name="main push gate (locality graph + hook scripts)",
        suite_step_failed=True,
        failed_steps=("Locality gate (full meizel check)",),
        check_steps=GATE_CHECK_STEPS,
    )
    unnamed = parse_failures(
        "meizel: 1 violations [unresolved-import: 1]",
        job_name="main push gate (locality graph + hook scripts)",
        suite_step_failed=True,
        failed_steps=("Install Python deps",),
        check_steps=GATE_CHECK_STEPS,
    )

    assert [failure.kind for failure in named] == [FAILURE_TEST]
    assert "Locality gate (full meizel check)" in named[0].test_id
    assert [failure.kind for failure in unnamed] == [FAILURE_INFRA]


# meiva:note
# A runner that never started a step is GitHub's fault, not `main`'s: the infra marker still wins,
# so a codeload outage cannot be read as a red of the tree.
def test_a_runner_that_never_started_stays_infra_even_for_the_gate() -> None:
    failures = parse_failures(
        "An action could not be found at the URI 'https://codeload.github.com/...'",
        job_name="main push gate (locality graph + hook scripts)",
        suite_step_failed=True,
        failed_steps=("Locality gate (full meizel check)",),
        check_steps=GATE_CHECK_STEPS,
    )

    assert [failure.kind for failure in failures] == [FAILURE_INFRA]


# meiva:note
# The step names are the contract between the workflow and the watch: renaming a step in
# `main-push.yml` would silently stop a failing check from naming itself, which is a hole no other
# test would see.
def test_the_gate_step_names_exist_in_the_workflow() -> None:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "main-push.yml").read_text())
    names = {step.get("name") for step in workflow["jobs"]["gate"]["steps"]}

    assert names >= GATE_CHECK_STEPS, GATE_CHECK_STEPS - names
