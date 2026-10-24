# meiva:note
# The words a stop prints, the gate it asks at and the rules for retiring extras: pure functions.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.stop.admission import confirmed, hosted_stop_refusal
from cli.commands.lifecycle.stop.extras_plan import retained_refusal, retires_helper
from cli.commands.lifecycle.stop.render import (
    completion_notes,
    data_plane_skip_line,
    incomplete_report,
    phase_timing,
    preview_lines,
)
from cli.commands.lifecycle.stop.steps import DataPlane, StopScope


def scope(
    *,
    force: bool,
    shells_kept: bool = False,
    browser_kept: bool = False,
    plane: DataPlane = DataPlane.KEPT,
) -> StopScope:
    return StopScope(
        force=force,
        shells_kept=shells_kept,
        browser_kept=browser_kept,
        data_plane=plane,
        tree_preserve=frozenset() if force else None,
    )


# meiva:note
# ── the confirm gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("answer", ["y", "y\n", " Y \n", "Y"])
def test_only_an_explicit_yes_proceeds(answer: str) -> None:
    assert confirmed(answer)


@pytest.mark.parametrize("answer", ["", "\n", "n\n", "yes\n", "yy", "sure"])
def test_anything_else_and_end_of_input_aborts(answer: str) -> None:
    assert not confirmed(answer)


# meiva:note
# ── a stop run from inside the work it drains ────────────────────────────────


def test_a_stop_inside_an_exec_domain_is_refused_and_points_at_a_login_shell() -> None:
    # meiva:note
    # Issue #2331: an exec-domain stop is SIGKILLed mid-drain with the call's process group,
    # and a stop (a restart's stop leg too) closes this unit's persistent shell sessions.
    refusal = hosted_stop_refusal("agent.execution.child", None)
    assert refusal is not None and "login shell" in refusal and "SIGKILLs" in refusal


def test_a_stop_inside_a_supervised_session_is_refused() -> None:
    refusal = hosted_stop_refusal(None, "meiva-agent-host")
    assert refusal == "stop must run outside the work it drains; use a login shell"


def test_the_exec_domain_is_named_before_the_session_that_hosts_it() -> None:
    assert hosted_stop_refusal("domain", "session") != hosted_stop_refusal(None, "session")


def test_a_stop_hosted_by_nothing_is_not_refused() -> None:
    assert hosted_stop_refusal(None, None) is None


# meiva:note
# ── the scope shown before the gate ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("shells_kept", "plane", "words"),
    [
        (False, DataPlane.STOPPED, "shell_sessions=closed; data plane=stopped"),
        (True, DataPlane.KEPT, "shell_sessions=retained; data plane=retained"),
        (False, DataPlane.NONE, "shell_sessions=closed; data plane=none on this host"),
    ],
)
def test_a_normal_stop_prints_one_header_line(
    shells_kept: bool, plane: DataPlane, words: str
) -> None:
    lines = preview_lines(
        scope(force=False, shells_kept=shells_kept, plane=plane), ("ignored",), "meiva-browser"
    )
    assert lines == (f"[meiva stop] local services; {words}",)


def test_a_force_stop_lists_what_it_stops_and_what_it_keeps() -> None:
    lines = preview_lines(
        scope(force=True, browser_kept=True, plane=DataPlane.STOPPED),
        ("meiva-agent-host", "meiva-gateway"),
        "meiva-browser",
    )
    assert lines == (
        "",
        "The following will be stopped:",
        "  service sessions: meiva-agent-host, meiva-gateway",
        "  shell sessions: closed",
        "  browser: kept up (meiva-browser, login session preserved)",
        "  infra (pg/redis): this cluster's own instance stopped (data preserved on disk)",
        "",
    )


def test_a_force_stop_with_nothing_to_list_says_so_and_omits_what_it_does_not_close() -> None:
    lines = preview_lines(
        scope(force=True, shells_kept=True, plane=DataPlane.NONE), (), "meiva-browser"
    )
    assert "  service sessions: (none)" in lines
    assert not any(line.startswith(("  shell sessions", "  browser")) for line in lines)
    assert "  infra (pg/redis): skipped (agent-runner uses the central node)" in lines


@pytest.mark.parametrize(
    ("plane", "words"),
    [
        (DataPlane.NONE, "skipped (agent-runner uses the central node)"),
        (DataPlane.KEPT, "kept up (keep_infra — migrate/start needs DB)"),
    ],
)
def test_a_force_stop_narrates_why_it_leaves_the_data_plane_alone(
    plane: DataPlane, words: str
) -> None:
    assert data_plane_skip_line(plane) == f"\n→ stop pg/redis: {words}"


# meiva:note
# ── how a stop ends ──────────────────────────────────────────────────────────


def test_an_escalated_shutdown_is_one_line_per_note_and_a_clean_stop_says_nothing() -> None:
    assert completion_notes(["postgres fast shutdown did not complete"]) == (
        "Stop completed with an escalation: postgres fast shutdown did not complete",
    )
    assert completion_notes([]) == ()


def test_a_failed_stop_names_each_phase_and_the_budget_it_consumed() -> None:
    phases = [("services", 1.26), ("shell-sessions", 12.0)]
    assert phase_timing(phases) == "services 1.3s; shell-sessions 12.0s"
    report = incomplete_report("boom", phases, "/home/run/lifecycle-op.json")
    assert report == (
        "Stop incomplete; services and the data plane were not force-killed: boom. "
        "phases: services 1.3s; shell-sessions 12.0s. "
        "Retry the command, or use meiva start (with this unit's explicit service selection) "
        "to resume. Status journal: /home/run/lifecycle-op.json."
    )


def test_a_stop_that_failed_before_its_first_phase_says_so() -> None:
    assert "phases: before the first phase." in incomplete_report("boom", [], "j")


def test_a_stop_that_failed_before_opening_a_journal_points_at_none() -> None:
    report = incomplete_report("boom", [], None)

    assert report.endswith("to resume.")
    assert "Status journal" not in report


# meiva:note
# ── retiring the extras ──────────────────────────────────────────────────────


@pytest.mark.parametrize("retained", [frozenset({"browser"}), frozenset({"pty-sessions"})])
def test_retiring_extras_refuses_while_a_unit_is_retained(retained: frozenset[str]) -> None:
    # meiva:note
    # The action, not only the command, refuses: retiring the helper needs the root gone.
    refusal = retained_refusal(retained)
    assert refusal is not None and "retains" in refusal and sorted(retained)[0] in refusal


def test_retiring_extras_is_admitted_with_nothing_retained() -> None:
    assert retained_refusal(frozenset()) is None


def test_the_permissions_helper_is_retired_on_macos_only() -> None:
    assert retires_helper("darwin")
    assert not retires_helper("linux")
