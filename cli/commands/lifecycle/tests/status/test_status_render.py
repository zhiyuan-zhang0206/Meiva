# meiva:note
# The wording of `meiva status`: banner, converge marker, host reading, drift, release, gateway.
#
# Each renderer takes the values the shell read and returns the lines it prints, so these
# tests assert the lines themselves. A blank line is an empty string.

from __future__ import annotations

from cli.commands.lifecycle.status.gateway import (
    GatewayReport,
    GatewayUnreachable,
    GatewayUnresolved,
    gateway_status_lines,
)
from cli.commands.lifecycle.status.render import (
    HostReading,
    HostReadingFailed,
    banner_lines,
    converge_lines,
    host_resource_lines,
    release_line,
    source_drift_lines,
)

# meiva:note
# ─── banner and converge marker ───────────────────────────────────────────────


def test_the_banner_names_the_checkout_and_the_sorted_capabilities() -> None:
    assert banner_lines("/repo", frozenset({"gateway", "agent-runner"})) == (
        "[meiva status] cwd = /repo  roles = agent-runner,gateway",
        "",
    )


def test_an_unresolved_role_reads_as_unknown() -> None:
    assert banner_lines("/repo", None) == ("[meiva status] cwd = /repo  roles = unknown", "")
    assert banner_lines("/repo", frozenset()) == ("[meiva status] cwd = /repo  roles = unknown", "")


def test_a_converged_home_prints_no_marker() -> None:
    assert converge_lines(None) == ()


def test_an_aborted_converge_prints_its_summary() -> None:
    assert converge_lines("2026-10-24T01:02:03+00:00 at step 'helper': keychain locked") == (
        "converge: INCOMPLETE 2026-10-24T01:02:03+00:00 at step 'helper': keychain locked",
    )


# meiva:note
# ─── host reading ─────────────────────────────────────────────────────────────


def test_a_host_reading_prints_cpu_memory_and_disk() -> None:
    reading = HostReading(
        cpu_pct=12.4,
        mem_pct=40.6,
        mem_used_gb=6.42,
        mem_total_gb=16.0,
        disk_pct=70.2,
        disk_used_gb=350.4,
        disk_total_gb=500.0,
    )

    assert host_resource_lines(reading) == (
        "  cpu 12%   memory 41% (6.4/16.0 GB)   disk 70% (350/500 GB)",
    )


def test_a_failed_host_reading_is_its_own_line() -> None:
    assert host_resource_lines(HostReadingFailed("no psutil here")) == (
        "  unavailable (no psutil here)",
    )


# meiva:note
# ─── prod source drift ────────────────────────────────────────────────────────


def test_a_prod_source_on_main_or_absent_prints_nothing() -> None:
    assert source_drift_lines(None) == ()


def test_a_detached_prod_source_is_the_expected_release_state() -> None:
    lines = source_drift_lines("HEAD")

    assert lines[0] == ""
    assert len(lines) == 2
    assert "detached at the released commit" in lines[1]
    assert "do not `checkout main`" in lines[1]
    assert "un-reviewed" not in lines[1]
    assert "Recover:" not in lines[1]


def test_a_prod_source_on_a_feature_branch_warns_and_names_the_recovery() -> None:
    lines = source_drift_lines("meiva-7/fix")

    assert lines[0] == ""
    assert lines[1].startswith(
        "⚠ prod source ($MEIVA_HOME/source) is on branch 'meiva-7/fix', not `main`."
    )
    assert "un-reviewed code on the next restart." in lines[1]
    assert lines[2].startswith("   Develop in a worktree, never the prod checkout. Recover:")
    assert "do not `checkout main`" in lines[2]
    assert len(lines) == 3


# meiva:note
# ─── release ──────────────────────────────────────────────────────────────────


def test_the_release_line_names_the_checkout_and_its_short_head() -> None:
    assert release_line("/repo", "c" * 40) == "release: source checkout /repo at ccccccc"


def test_an_unreadable_head_prints_as_unreadable() -> None:
    assert release_line("/repo", None) == "release: source checkout /repo at unreadable HEAD"


# meiva:note
# ─── gateway view ─────────────────────────────────────────────────────────────

_HEADING = ("", "gateway cluster status (GET /api/cluster/status):")


def test_a_gateway_report_prints_name_capabilities_and_pause() -> None:
    report = GatewayReport(
        machine_name="station-a", serves="observability-station", paused=False, paused_reason=None
    )

    assert gateway_status_lines(report) == (
        *_HEADING,
        "  machine_name: station-a",
        "  serves:       observability-station",
        "  paused:       False",
    )


def test_a_paused_gateway_names_the_clause_that_paused_it() -> None:
    report = GatewayReport(
        machine_name="host-a", serves="gateway", paused=True, paused_reason="startup"
    )

    assert gateway_status_lines(report)[-1] == "  paused:       True (startup)"


def test_an_unreachable_gateway_prints_inline() -> None:
    assert gateway_status_lines(GatewayUnreachable("connection refused")) == (
        *_HEADING,
        "  ✗ gateway unreachable: connection refused",
    )


def test_an_unresolvable_gateway_url_prints_inline() -> None:
    assert gateway_status_lines(GatewayUnresolved("gateway_url unset")) == (
        *_HEADING,
        "  ✗ cannot resolve gateway URL: gateway_url unset",
    )
