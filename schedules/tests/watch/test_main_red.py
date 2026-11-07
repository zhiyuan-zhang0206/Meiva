# meiva:note
# The main-red watch's decisions, replayed against the two real failures of 2026-11-01: one that
# the nightly passed on the same commit an hour later, and one that no run explained.
# meiva:link schedules/README.md

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schedules.plans.watch.workflow_graph import downstream_failing_jobs
from schedules.plans.watch.main_red import (
    FAILURE_INFRA,
    GATE_CHECK_STEPS,
    FAILURE_TEST,
    FAILURE_UNNAMED,
    NIGHTLY_STALE_AFTER,
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
_MACOS = "ci.yml"
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


# meiva:note
# `main` can stand still for days with nobody merging, and the two timed workflows then run on the
# same commit. Two runs of the same fingerprint is already two sightings -- the round only reads
# runs newer than its watermark -- so the card must not wait for the tree to move.
def test_the_same_red_twice_on_one_commit_confirms() -> None:
    first = apply_run({}, _red(_NIGHTLY, "aaa111", "2026-11-02T00:00:00+00:00", _WATCH_WORK))
    second = apply_run(
        dict(first.entries), _red(_NIGHTLY, "aaa111", "2026-11-03T00:00:00+00:00", _WATCH_WORK)
    )

    assert first.confirmed == ()
    assert len(second.confirmed) == 1
    assert second.confirmed[0].red_count == 2


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
    assert any("had no failing job to parse" in note for note in plan.notes)


# meiva:note
# The positive control for the hole 2026-11-07 exposed: a release-gate structure lane failing on
# `[note-link]` prints no `FAILED path::node`, and the step is in no workflow's check list, so this
# used to return an empty tuple -- the run was red and the watch recorded nothing. The failing
# steps are now the red's name, and the class says the watch could not classify it.
def test_a_red_the_watch_cannot_name_still_names_itself() -> None:
    log = (
        "base/agents/context/external_attachment/plugin_state_codec.py:48: "
        "[note-link] not an exact tracked path: meiva/impersonation/docs/attachment.meiva.okf.md\n"
        "##[error]Process completed with exit code 1.\n"
    )
    failures = parse_failures(
        log,
        job_name="Full static checks (structure)",
        suite_step_failed=True,
        failed_steps=("Validate source notes, structural hooks and codegen freshness",),
    )

    assert [failure.kind for failure in failures] == [FAILURE_UNNAMED]
    assert "Validate source notes" in failures[0].test_id


# meiva:note
# One card per unreadable shape, opened on the first red run rather than the second: two strikes
# answer "is this case flaky?", and that question does not exist when the doubt is whether the
# watch can read the log at all.
def test_an_unnameable_red_opens_one_card_and_counts_the_rest() -> None:
    failure = Failure(
        "Full static checks (structure): unnamed failure (Validate source notes, structural hooks "
        "and codegen freshness)",
        FAILURE_UNNAMED,
    )
    first = apply_run(
        {},
        RunFacts(_GATE, "sha1", datetime(2026, 11, 7, 15, 36, tzinfo=UTC), "failure", (failure,)),
    )
    entries = dict(first.entries)
    assert [entry.state for entry in entries.values()] == [STATE_CONFIRMED]
    assert [entry.kind for entry in entries.values()] == [FAILURE_UNNAMED]
    assert len(first.confirmed) == 1

    second = apply_run(
        entries,
        RunFacts(_GATE, "sha2", datetime(2026, 11, 7, 21, 36, tzinfo=UTC), "failure", (failure,)),
    )
    assert second.confirmed == ()
    assert [entry.red_count for entry in dict(second.entries).values()] == [2]

    # meiva:note
    # The card's exit condition has to be a predicate the watch itself evaluates, or the card joins
    # the bare entries nobody ever closes: `covers_full_suite` and `last_red_at` are both fields of
    # this entry's own record, so the reader can check the statement against the same numbers.
    body = card_body(
        entries[first.entries[0][0]],
        latest_url=None,
        job_names=("Full static checks (structure)",),
        candidate_commits=(),
    )
    assert "Class: `unnamed`" in body
    assert "covers_full_suite=true" in body and "last_red_at" in body
    assert "A lane that runs part of the tree is retired only by its own green" in body


# meiva:note
# Neither half of the invariant is worth anything without the negative control: a green run must
# stay silent, so the new class cannot turn ordinary green days into cards.
def test_a_green_run_names_nothing_and_closes_an_unnameable_red() -> None:
    failure = Failure("job: unnamed failure (Install Python deps)", FAILURE_UNNAMED)
    red = apply_run(
        {},
        RunFacts(_GATE, "sha1", datetime(2026, 11, 7, 15, 36, tzinfo=UTC), "failure", (failure,)),
    )
    green = apply_run(
        dict(red.entries),
        RunFacts(_GATE, "sha2", datetime(2026, 11, 7, 21, 36, tzinfo=UTC), "success", ()),
    )

    assert red.entries != ()
    assert green.entries == ()
    assert green.confirmed == ()


# meiva:note
# The 2026-11-06 job log lines, verbatim: every line carries the run's timestamp, and xdist's
# progress lines carry a worker label too. This is the positive control for the parser -- the same
# bytes that made five hours of red `main` invisible must now name six failing cases.
def test_the_shapes_the_job_log_delivers_are_read() -> None:
    delivered = (
        "2026-11-06T00:37:36.2030925Z FAILED "
        "tests/e2e/flow/test_shell_effects.py::test_session_verbs_run_in_one_shell_and_change_its_lifetime"
        " - AssertionError: Timed out after 90.06s\n"
        "2026-11-06T00:32:24.7760416Z [gw1] [ 24%] FAILED "
        "tests/e2e/plugins/test_meiva_code_effects.py::test_cwd_survives_a_restart - AssertionError\n"
        "2026-11-06T00:37:36.4758780Z FAILED "
        "tests/e2e/lifecycle/test_budget_handoff.py::"
        "test_usage_reminder_preserves_handoff_across_late_checkpoint_and_restart[dynamic workflow "
        "orchestrator] - AssertionError\n"
    )

    parsed = parse_failures(
        delivered, job_name="Measure e2e durations (1/4)", suite_step_failed=True
    )

    assert [failure.kind for failure in parsed] == [FAILURE_TEST] * 3, parsed
    assert parsed[0].test_id == (
        "tests/e2e/flow/test_shell_effects.py::"
        "test_session_verbs_run_in_one_shell_and_change_its_lifetime"
    )
    assert parsed[1].test_id == (
        "tests/e2e/plugins/test_meiva_code_effects.py::test_cwd_survives_a_restart"
    )
    # meiva:note
    # A parametrized id ends at the first space, so the bracket never reaches the fingerprint and a
    # case does not split into one fingerprint per parameter set.
    assert parsed[2].test_id == (
        "tests/e2e/lifecycle/test_budget_handoff.py::"
        "test_usage_reminder_preserves_handoff_across_late_checkpoint_and_restart[dynamic"
    )


# meiva:note
# A suite that ran and printed a summary this parser could not read is the watch's own blindness,
# not the runner's fault -- so it reports as `unnamed` on the first sighting rather than as infra
# noise nobody is paged for.
def test_a_pytest_summary_that_names_no_case_names_the_watch_instead() -> None:
    log = (
        "2026-11-06T00:37:36.2029581Z ============ short test summary info ============\n"
        "2026-11-06T00:37:36.2030925Z ================== no tests ran ==================\n"
    )

    parsed = parse_failures(log, job_name="Backend", suite_step_failed=True)

    assert [failure.kind for failure in parsed] == [FAILURE_UNNAMED]
    assert parsed[0].test_id == (
        "Backend: pytest summary without a parsable case (no failed step reported)"
    )


def test_both_failure_line_shapes_are_read() -> None:
    pytest_shape = "FAILED tests/x/test_y.py::test_z - AssertionError"
    native_shape = "  FAILED package.module::test_z"

    assert parse_failures(pytest_shape, job_name="j", suite_step_failed=True)[0].test_id == (
        "tests/x/test_y.py::test_z"
    )
    assert parse_failures(native_shape, job_name="j", suite_step_failed=True)[0].test_id == (
        "package.module::test_z"
    )


# meiva:note
# `ci.yml`'s nightly verdict carries the macOS data-plane job, which runs two directories rather than
# the population. A red it reports is that job's: the next Linux nightly never collected the test, so
# before the failure carried `covers_full_suite` the card retired while the red stood.
def _partial_red(workflow: str, sha: str, when: str, *test_ids: str) -> RunFacts:
    return RunFacts(
        workflow=workflow,
        head_sha=sha,
        created_at=datetime.fromisoformat(when),
        conclusion="failure",
        failures=tuple(
            Failure(test_id, FAILURE_TEST, covers_full_suite=False) for test_id in test_ids
        ),
    )


def _partial_green(workflow: str, sha: str, when: str) -> RunFacts:
    return RunFacts(
        workflow=workflow,
        head_sha=sha,
        created_at=datetime.fromisoformat(when),
        conclusion="success",
        failures=(),
    )


def test_a_partial_job_red_is_not_retired_by_the_populations_green() -> None:
    first = apply_run({}, _partial_red(_MACOS, "aaa111", "2026-11-08T21:47:00+00:00", _WATCH_WORK))
    confirmed = apply_run(
        dict(first.entries),
        _partial_red(_MACOS, "bbb222", "2026-11-09T21:47:00+00:00", _WATCH_WORK),
    )
    (entry,) = dict(confirmed.entries).values()
    assert confirmed.confirmed and entry.covers_full_suite is False

    later_green = apply_run(
        dict(confirmed.entries), _green(_NIGHTLY, "ccc333", "2026-11-10T00:00:00+00:00")
    )

    assert dict(later_green.entries) == dict(confirmed.entries)
    assert later_green.closes == ()


def test_a_partial_job_red_is_retired_by_its_own_green() -> None:
    first = apply_run({}, _partial_red(_MACOS, "aaa111", "2026-11-08T21:47:00+00:00", _WATCH_WORK))
    confirmed = apply_run(
        dict(first.entries),
        _partial_red(_MACOS, "bbb222", "2026-11-09T21:47:00+00:00", _WATCH_WORK),
    )
    (entry,) = dict(confirmed.entries).values()
    assert entry.task_id is None

    own_green = apply_run(
        dict(confirmed.entries), _partial_green(_MACOS, "ccc333", "2026-11-10T21:47:00+00:00")
    )

    assert dict(own_green.entries) == {}


def test_a_full_suite_red_is_still_retired_by_any_full_suite_green() -> None:
    # meiva:note
    # The interchangeable-evidence rule of 2026-11-01 stands where it was written: both workflows
    # run the whole population, so one green explains the other's red on the same tree.
    red = apply_run({}, _red(_GATE, "aaa111", "2026-11-02T00:00:00+00:00", _WATCH_WORK))
    green = apply_run(dict(red.entries), _green(_NIGHTLY, "bbb222", "2026-11-02T06:00:00+00:00"))

    assert dict(green.entries) == {}


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
        "nightly", now - timedelta(hours=5), now, stale_after=NIGHTLY_STALE_AFTER
    )
    stale = liveness_alert(
        "nightly", now - timedelta(hours=30), now, stale_after=NIGHTLY_STALE_AFTER
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
    # meiva:note
    # 2026-11-07 changed what an unnamed step means. It used to be infra -- "installs fail for
    # reasons that are nobody's regression" -- and that judgement is what let a red structure lane
    # on `main` leave no record at all. It now reports as `unnamed`, naming the step, and the step
    # name in the fingerprint is the list a mapping would have to cover.
    assert [failure.kind for failure in unnamed] == [FAILURE_UNNAMED]
    assert "Install Python deps" in unnamed[0].test_id


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


# meiva:note
# A stale note that stops at "check whether GitHub delayed it" hands the reader a diagnosis and no
# action. The stream that knows what its own workflow costs supplies the make-up command, and the
# note ends with it; a stream that never supplied one keeps exactly the note it had before.
def test_a_stale_note_ends_with_the_make_up_the_stream_supplies() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    supplied = liveness_alert(
        "nightly-full",
        now - timedelta(hours=30),
        now,
        stale_after=NIGHTLY_STALE_AFTER,
        make_up="`gh workflow run ci.yml --ref main` (full suite).",
    )
    absent = liveness_alert(
        "nightly", now - timedelta(hours=30), now, stale_after=NIGHTLY_STALE_AFTER
    )

    assert supplied is not None
    assert supplied.endswith("Make-up: `gh workflow run ci.yml --ref main` (full suite).")
    assert absent is not None and "Make-up" not in absent


# meiva:note
# One run, three red jobs, one event (405, 2026-11-08: `#6476`/`#6477`/`#6478`). The shard is the
# root; the two jobs after it in the graph only report that the shard failed. `needs` says so, and
# the folded names still reach the card through `job_names`.
_WORKFLOW = """
jobs:
  classify:
    name: classify change
  e2e-shard:
    name: e2e shard (${{ matrix.group }}/4)
    needs: [classify]
  e2e:
    name: e2e (Playwright happy path)
    needs: [classify, e2e-shard]
  e2e-gate:
    name: e2e gate
    needs: [classify, e2e]
"""
_SHARD = "e2e shard (3/4)"
_HAPPY = "e2e (Playwright happy path)"
_GATE_JOB = "e2e gate"


def test_the_workflow_needs_fold_the_jobs_downstream_of_a_red() -> None:
    assert downstream_failing_jobs(_WORKFLOW, (_SHARD, _HAPPY, _GATE_JOB)) == (_HAPPY, _GATE_JOB)


def test_two_shards_of_one_matrix_are_not_downstream_of_each_other() -> None:
    # meiva:note
    # The false suppression that timing would have caused: on 2026-11-07 run 42418213748 shards 1,
    # 2 and 4 finished by 22:03:54 and shard 3 only started at 22:07:22, because five suites share
    # four workers. Both shards are real reds and neither `needs` the other.
    assert downstream_failing_jobs(_WORKFLOW, ("e2e shard (1/4)", _SHARD)) == ()


def test_an_unreadable_workflow_folds_nothing() -> None:
    assert downstream_failing_jobs("", (_HAPPY,)) == ()
    assert downstream_failing_jobs("jobs: [", (_HAPPY,)) == ()
    assert downstream_failing_jobs("jobs: {}", (_HAPPY,)) == ()


def test_a_downstream_job_is_folded_into_one_card_and_still_named() -> None:
    run = RunFacts(
        workflow=_MACOS,
        head_sha=_SHA,
        created_at=datetime.fromisoformat("2026-11-07T22:40:47+00:00"),
        conclusion="failure",
        failures=(
            Failure(f"{_SHARD}: unnamed failure (Run e2e tests)", FAILURE_UNNAMED, _SHARD),
            Failure(f"{_HAPPY}: unnamed failure (Verify shard results)", FAILURE_UNNAMED, _HAPPY),
            Failure(
                f"{_GATE_JOB}: unnamed failure (Report the end-to-end suite outcome)",
                FAILURE_UNNAMED,
                _GATE_JOB,
            ),
        ),
        job_names=(_SHARD, _HAPPY, _GATE_JOB),
        downstream_jobs=(_HAPPY, _GATE_JOB),
    )
    plan = apply_run({}, run)
    assert [entry.fingerprint for _, entry in plan.entries] == [
        fingerprint(_MACOS, f"{_SHARD}: unnamed failure (Run e2e tests)")
    ]
    assert len(plan.confirmed) == 1
    body = card_body(
        plan.confirmed[0],
        latest_url="https://example.invalid/run",
        job_names=run.job_names,
        candidate_commits=(),
    )
    assert _HAPPY in body
    assert _GATE_JOB in body


def test_a_named_failure_is_never_folded_as_downstream() -> None:
    run = RunFacts(
        workflow=_MACOS,
        head_sha=_SHA,
        created_at=datetime.fromisoformat("2026-11-07T22:40:47+00:00"),
        conclusion="failure",
        failures=(
            Failure(_SEMAPHORE, FAILURE_TEST, _SHARD),
            Failure(f"{_HAPPY}: unnamed failure (Verify shard results)", FAILURE_UNNAMED, _HAPPY),
        ),
        downstream_jobs=(_HAPPY,),
    )
    entries = dict(apply_run({}, run).entries)
    assert list(entries) == [fingerprint(_MACOS, _SEMAPHORE)]
    # meiva:note
    # A named failure from a downstream job is evidence in its own right, so it is never folded.
    named_downstream = RunFacts(
        workflow=_MACOS,
        head_sha=_SHA,
        created_at=datetime.fromisoformat("2026-11-07T22:40:47+00:00"),
        conclusion="failure",
        failures=(Failure(_SEMAPHORE, FAILURE_TEST, _HAPPY),),
        downstream_jobs=(_HAPPY,),
    )
    assert len(dict(apply_run({}, named_downstream).entries)) == 1
