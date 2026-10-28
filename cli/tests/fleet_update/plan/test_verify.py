# meiva:note
# What `up` concludes from the roster and the per-host answers: which machines it waits for, how
# long to keep waiting, which smokes run and which hosts are reported skipped, and the line each
# listed host ends with. The inputs are what the gateway's roster program printed and what each
# step returned.
# meiva:link cli/docs/fleet-update.meiva.okf.md
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cli.fleet_update.plan.host_failures import (
    HostFailure,
    incomplete_summary,
    outcome,
    start_failure,
)
from cli.fleet_update.plan.roster import (
    RosterRow,
    missing_rows,
    parse_roster,
    poll_delay,
    snapshot,
    timeout_message,
    unlisted_lines,
)
from cli.fleet_update.plan.verify_steps import (
    DriftCheck,
    SmokeStep,
    drift_checks,
    duplicate_names,
    machine_name,
    smoke_plan,
)

_SHA = "a" * 40


def _row(
    name: str,
    *,
    online: bool = True,
    mismatch: bool = False,
    head: str | None = _SHA,
    running: str | None = _SHA,
    serves: bool = True,
) -> RosterRow:
    return RosterRow(name, online, mismatch, head, running, serves)


def _wire(row: RosterRow) -> dict[str, object]:
    return {
        "name": row.name,
        "online": row.online,
        "identity_mismatch": row.identity_mismatch,
        "head_sha": row.head_sha,
        "running_sha": row.running_sha,
        "serve_agent_runner": row.serve_agent_runner,
    }


def test_the_roster_is_the_last_line_the_program_printed() -> None:
    rows = (_row("gw"), _row("laptop", online=False, head=None, running=None))
    output = "a warning\n" + json.dumps([_wire(row) for row in rows])
    assert parse_roster(output) == rows


@pytest.mark.parametrize(
    "output",
    ["", "not json", '{"name": "gw"}', '[{"name": "gw"}]', '[{"name": 7, "online": true}]', "[1]"],
)
def test_a_reply_that_is_no_roster_is_a_value_error_with_the_replys_kind(output: str) -> None:
    with pytest.raises(ValueError, match="roster"):
        parse_roster(output)


def test_a_listed_machine_with_no_row_is_missing() -> None:
    rows = (_row("gw"), _row("lin"))
    assert missing_rows(rows, frozenset({"gw", "mac", "lin", "bsd"})) == ("bsd", "mac")
    assert missing_rows(rows, frozenset({"gw"})) == ()


def test_a_roster_where_every_listed_machine_agrees_is_settled() -> None:
    seen = snapshot((_row("gw"), _row("lin")), frozenset({"gw", "lin"}), frozenset())
    assert seen.settled and seen.commit == _SHA and seen.offline == ()


def test_an_offline_or_mismatched_machine_unsettles_the_roster() -> None:
    wanted = frozenset({"gw", "lin"})
    offline = snapshot((_row("gw"), _row("lin", online=False)), wanted, frozenset())
    assert not offline.settled and offline.offline == ("lin",)
    mismatch = snapshot((_row("gw"), _row("lin", mismatch=True)), wanted, frozenset())
    assert not mismatch.settled and mismatch.offline == ("lin",)


def test_a_machine_running_other_code_than_its_checkout_unsettles_the_roster() -> None:
    seen = snapshot(
        (_row("gw"), _row("lin", running="b" * 40)), frozenset({"gw", "lin"}), frozenset()
    )
    assert not seen.settled and seen.offline == ()


def test_machines_on_different_commits_are_not_settled() -> None:
    seen = snapshot(
        (_row("gw"), _row("lin", head="b" * 40, running="b" * 40)),
        frozenset({"gw", "lin"}),
        frozenset(),
    )
    assert not seen.settled and len(seen.commits) == 2


def test_a_host_this_run_knows_failed_is_not_waited_for() -> None:
    rows = (_row("gw"), _row("lin", online=False, head="b" * 40, running="b" * 40))
    seen = snapshot(rows, frozenset({"gw", "lin"}), frozenset({"lin"}))
    assert seen.settled and seen.offline == () and seen.commit == _SHA


def test_a_machine_with_no_commit_is_never_settled() -> None:
    seen = snapshot((_row("gw", head=None, running=None),), frozenset({"gw"}), frozenset())
    assert not seen.settled


def test_machines_the_window_was_not_asked_about_are_reported_not_required() -> None:
    rows = (_row("gw"), _row("laptop", online=False, head=None, running=None))
    assert snapshot(rows, frozenset({"gw"}), frozenset()).settled
    assert unlisted_lines(rows, frozenset({"gw"})) == (
        "roster: laptop is not listed, not checked: online=False head=None running=None",
    )


def test_the_timeout_names_the_offline_machines_and_the_commits_seen() -> None:
    seen = snapshot((_row("gw", online=False), _row("lin")), frozenset({"gw", "lin"}), frozenset())
    assert timeout_message(30, seen) == (
        f"roster after 30s: offline ['gw']; commits {{('{_SHA}', '{_SHA}')}}"
    )


@pytest.mark.parametrize(
    ("elapsed", "delay"), [(0.0, 5.0), (29.9, 5.0), (30.0, None), (31.0, None)]
)
def test_the_roster_is_read_again_after_a_poll_until_the_window_is_spent(
    elapsed: float, delay: float | None
) -> None:
    assert poll_delay(elapsed, 30, 5.0) == delay


def test_each_serving_machine_gets_one_smoke_run_by_the_host_that_owns_it() -> None:
    names = {"gw": "gw", "mac": "mac-mini", "lin": "lin"}
    rows = (_row("gw"), _row("mac-mini"), _row("lin", serves=False))
    assert smoke_plan(rows, names, frozenset({"gw", "mac", "lin"})) == (
        SmokeStep("gw", "gw", runs=True),
        SmokeStep("mac", "mac-mini", runs=True),
    )


def test_a_machine_whose_host_never_started_is_skipped_not_smoked() -> None:
    names = {"gw": "gw", "lin": "lin"}
    rows = (_row("gw"), _row("lin"))
    assert smoke_plan(rows, names, frozenset({"gw"})) == (
        SmokeStep("gw", "gw", runs=True),
        SmokeStep("lin", "lin", runs=False),
    )


def test_the_drift_checks_are_the_gateways_schedules_then_every_hosts_plugins() -> None:
    assert drift_checks("gw", ("gw", "mac")) == (
        DriftCheck("gw", "schedules verify --no-notify"),
        DriftCheck("gw", "plugins verify"),
        DriftCheck("mac", "plugins verify"),
    )


def test_a_machine_name_is_the_last_word_a_host_printed() -> None:
    assert machine_name("warning\nmac-mini\n") == "mac-mini"
    assert machine_name("  \n") is None
    assert machine_name("") is None


def test_two_hosts_reporting_one_machine_name_are_refused() -> None:
    assert duplicate_names({"a": "x", "b": "y"}) is None
    assert duplicate_names({"a": "x", "b": "x"}) == (
        "two listed hosts report one machine name: {'a': 'x', 'b': 'x'}"
    )


_INCOMPLETE = json.dumps(
    {
        "at": "2026-10-18T10:02:33+00:00",
        "step": "permissions helper build + sign + load",
        "error": "the keychain is locked",
    }
)


# meiva:note
# The window an attempt occupied, in the same frame the marker stamps itself (UTC here). A
# marker inside it is this attempt's; one outside it is an earlier wave's leftover.
_THIS_ATTEMPT = (
    datetime(2026, 10, 18, 10, 2, 0, tzinfo=UTC),
    datetime(2026, 10, 18, 10, 3, 0, tzinfo=UTC),
)
_EARLIER_ATTEMPT = (
    datetime(2026, 10, 18, 10, 10, 0, tzinfo=UTC),
    datetime(2026, 10, 18, 10, 11, 0, tzinfo=UTC),
)


def test_a_host_whose_converge_aborted_is_named_by_the_step_it_recorded() -> None:
    assert start_failure("lin", _INCOMPLETE, "lin: exit 1: boom", window=_THIS_ATTEMPT) == (
        HostFailure("lin", "permissions helper build + sign + load", "the keychain is locked")
    )


def test_a_marker_from_an_earlier_wave_does_not_become_this_attempts_cause() -> None:
    # meiva:note
    # The marker outlives its wave; attributing this start's failure to the earlier step would
    # send the reader after a cause that is already stale. It falls back to the start's words.
    assert start_failure("lin", _INCOMPLETE, "lin: exit 1: boom", window=_EARLIER_ATTEMPT) == (
        HostFailure("lin", "start", "lin: exit 1: boom")
    )


def test_a_marker_stamped_without_a_zone_is_not_evidence_about_any_window() -> None:
    naive = json.dumps({"at": "2026-10-18T10:02:33", "step": "s", "error": "e"})
    assert start_failure("lin", naive, "lin: exit 1: boom", window=_THIS_ATTEMPT) == (
        HostFailure("lin", "start", "lin: exit 1: boom")
    )


@pytest.mark.parametrize("recorded", [None, "", "not json", "[]", '{"step": "x"}'])
def test_a_host_with_no_readable_record_fails_at_its_start_with_the_starts_own_words(
    recorded: str | None,
) -> None:
    assert start_failure("lin", recorded, "lin: exit 1: boom", window=_THIS_ATTEMPT) == (
        HostFailure("lin", "start", "lin: exit 1: boom")
    )


def test_every_listed_host_ends_with_serving_its_failures_or_why_it_was_skipped() -> None:
    failures = (HostFailure("mac", "smoke", "exit 2"), HostFailure("mac", "plugins verify", "red"))
    skipped = {"lin": "start failed", "bsd": "dry run"}
    assert outcome("gw", failures, skipped) == "serving"
    assert outcome("mac", failures, skipped) == "failed at 'smoke': exit 2; 'plugins verify': red"
    assert outcome("lin", failures, skipped) == "skipped: start failed"
    assert (
        outcome("lin", (HostFailure("lin", "start", "boom"),), skipped) == "failed at 'start': boom"
    )


def test_a_partial_up_is_summarised_one_line_per_failure_then_the_count() -> None:
    failures = (HostFailure("lin", "start", "boom"), HostFailure("mac", "smoke", "red"))
    assert incomplete_summary(failures) == (
        "PARTIAL: lin failed at 'start': boom",
        "PARTIAL: mac failed at 'smoke': red",
        "up incomplete: 2 host(s) failed; the rest are serving. Fix each and rerun `up` (idempotent).",
    )
