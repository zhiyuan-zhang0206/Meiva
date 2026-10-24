# meiva:note
# The pre-stop start-readiness gate (task #3156; restart caller #3165), from facts to verdict.
#
# `meiva start`'s read-only local checks are moved in front of the stop: a failure HERE refuses the
# stop while the host still serves, instead of failing on a stopped host. These tests pin each check
# family's disposition (fatal: refuse, or observation: report and proceed) over the facts the shell
# reads. The shell that reads them from a real home is exercised in
# `startup/test_start_readiness_preflight.py`; the caller-level refusal contract lives in the
# restart tests.

from __future__ import annotations

from dataclasses import replace

from cli.commands.lifecycle.start.preflight_findings import (
    EntrypointFacts,
    PortFacts,
    PortOccupant,
    PreflightFacts,
    PreflightReport,
    TreeFacts,
    preflight_report,
)


# meiva:note
# `dataclasses.replace` checks each keyword against its own field type at run time only, so the
# open `**changes` of these builders cannot be typed statically; a misspelt field raises.
def _tree(root: str, **changes: object) -> TreeFacts:
    return replace(TreeFacts(root, None, (), None), **changes)  # pyright: ignore[reportArgumentType]


# meiva:note
# Same open keyword bag as `_tree`: `replace` rejects an unknown field at run time.
def _python(**changes: object) -> EntrypointFacts:
    return replace(
        EntrypointFacts(
            path="/repo/.venv/bin/python",
            label="the interpreter every service session launches through",
            fix="re-run `uv sync`",
            is_file=True,
            executable=True,
        ),
        **changes,  # pyright: ignore[reportArgumentType]
    )


# meiva:note
# A home the gate passes: three healthy trees, a launchable interpreter, nothing occupied.
def _clean(**changes: object) -> PreflightFacts:
    facts = PreflightFacts(
        checkout_problem=None,
        ports=PortFacts(roles_resolved=True, check_error=None, occupied=()),
        trees=(_tree("/h/logs"), _tree("/h/workspaces"), _tree("/h/memory")),
        not_directories=(),
        marker="/h/logs/.metadata_never_index",
        marker_problem=None,
        migration_error=None,
        unreadable_migrations=(),
        entrypoints=(_python(),),
    )
    return replace(facts, **changes)  # pyright: ignore[reportArgumentType]


def _fatal(report: PreflightReport) -> str:
    return "\n".join(report.stderr)


def test_a_clean_home_passes_and_says_so() -> None:
    report = preflight_report(_clean())

    assert report == PreflightReport(
        stdout=("  ✓ start-readiness preflight passed",), stderr=(), exit_code=0
    )


# meiva:note
# `meiva start` refuses a prod home launched from a dev checkout; that refusal must be
# reachable before the stop.
def test_a_prod_checkout_violation_is_fatal() -> None:
    report = preflight_report(_clean(checkout_problem="prod home launched from a disposable one"))

    assert report.exit_code == 1
    assert "prod home launched from a disposable one" in _fatal(report)


# meiva:note
# An unresolvable identity is the probe gate's refusal, not this one's: the port checks are
# skipped and the update still proceeds.
def test_an_unresolved_machine_role_skips_the_ports_as_an_observation() -> None:
    ports = PortFacts(roles_resolved=False, check_error=None, occupied=())

    report = preflight_report(_clean(ports=ports))

    assert report.exit_code == 0
    assert "machine role did not resolve — port checks skipped" in "\n".join(report.stdout)


# meiva:note
# A preflight must never be the thing that takes the host down.
def test_a_failed_port_detection_is_an_observation_never_a_refusal() -> None:
    ports = PortFacts(roles_resolved=True, check_error="no such service", occupied=())

    report = preflight_report(_clean(ports=ports))

    assert report.exit_code == 0
    assert "  ! health-port check skipped: no such service" in report.stdout


# meiva:note
# A terminal occupant on a daemon health port refuses `meiva start` (#977); the same verdict
# must refuse the stop.
def test_health_port_occupancy_is_fatal() -> None:
    occupant = PortOccupant(
        "meiva-gateway", "answered by $MEIVA_HOME=/other-unit (http://127.0.0.1:8123/healthz)"
    )
    ports = PortFacts(roles_resolved=True, check_error=None, occupied=(occupant,))

    report = preflight_report(_clean(ports=ports))

    assert report.exit_code == 1
    assert "meiva-gateway" in _fatal(report)
    assert "other-unit" in _fatal(report)
    assert "#977" in _fatal(report)


def test_a_private_tree_root_converge_would_abort_on_is_fatal() -> None:
    trees = (_tree("/h/logs", root_problem="is a symlink"), _tree("/h/workspaces"))

    report = preflight_report(_clean(trees=trees))

    assert report.exit_code == 1
    assert "/h/logs is a symlink" in _fatal(report)
    assert "converge aborts the whole start on this root" in _fatal(report)


def test_the_metadata_marker_must_be_a_regular_file() -> None:
    report = preflight_report(_clean(marker_problem="is not a regular file"))

    assert report.exit_code == 1
    assert "/h/logs/.metadata_never_index is not a regular file" in _fatal(report)


# meiva:note
# The 2026-09-12 class: converge skips these, so they cannot fail a start, but they are
# reported before the stop instead of only in the converge log.
def test_a_non_regular_node_inside_a_tree_is_an_observation() -> None:
    trees = (_tree("/h/workspaces", skipped=("/h/workspaces/leftover.sock",)),)

    report = preflight_report(_clean(trees=trees))

    assert report.exit_code == 0
    assert report.stdout[0] == "\n→ start-readiness observations (not blocking):"
    assert "/h/workspaces/leftover.sock is a socket/FIFO/device" in "\n".join(report.stdout)
    assert "converge skips it" in "\n".join(report.stdout)


# meiva:note
# A preflight must never take an update down: a scan that cannot run is reported and the
# stop proceeds.
def test_a_scan_that_could_not_run_is_an_observation() -> None:
    trees = (_tree("/h/logs", scan_error="permission denied"),)

    report = preflight_report(_clean(trees=trees))

    assert report.exit_code == 0
    assert "  ! could not scan /h/logs for non-regular files: permission denied" in report.stdout


def test_configs_and_secrets_that_mkdir_cannot_tolerate_are_fatal() -> None:
    report = preflight_report(_clean(not_directories=("/h/configs",)))

    assert report.exit_code == 1
    assert "/h/configs exists but is not a directory" in _fatal(report)


# meiva:note
# The apply-side vet: a tracked migration the applier cannot open would fail
# `meiva start`'s apply on the stopped host.
def test_an_unreadable_tracked_migration_is_fatal() -> None:
    unreadable = (("20260912T010000_x", "Permission denied: 'x.sql'"),)

    report = preflight_report(_clean(unreadable_migrations=unreadable))

    assert report.exit_code == 1
    assert "migrations/20260912T010000_x is not readable (Permission denied: 'x.sql')" in _fatal(
        report
    )


def test_a_migration_directory_that_cannot_be_enumerated_is_fatal() -> None:
    report = preflight_report(_clean(migration_error="migrations dir does not exist: /x"))

    assert report.exit_code == 1
    assert "migrations/ cannot be enumerated: migrations dir does not exist: /x" in _fatal(report)


# meiva:note
# Task #3165 (D4): every service session launches through `.venv/bin/python`; a damaged
# venv is the restart verb's real "stopped and cannot come back" class.
def test_a_missing_interpreter_is_fatal_with_its_repair() -> None:
    report = preflight_report(_clean(entrypoints=(_python(is_file=False, executable=False),)))

    assert report.exit_code == 1
    assert "/repo/.venv/bin/python is missing" in _fatal(report)
    assert "re-run `uv sync`" in _fatal(report)


def test_an_interpreter_without_the_exec_bit_is_fatal() -> None:
    report = preflight_report(_clean(entrypoints=(_python(executable=False),)))

    assert report.exit_code == 1
    assert "/repo/.venv/bin/python is not executable" in _fatal(report)


# meiva:note
# One refusal lists every reason, so the operator fixes the set in one pass.
def test_all_fatal_findings_are_reported_together_after_the_observations() -> None:
    report = preflight_report(
        _clean(
            checkout_problem="checkout problem",
            trees=(_tree("/h/memory", root_problem="is not a directory", skipped=("/h/n",)),),
            entrypoints=(_python(is_file=False),),
        )
    )

    assert report.exit_code == 1
    assert report.stderr[0].startswith("\n✗ start-readiness preflight: 3 problem(s)")
    assert [line.strip() for line in report.stderr[1:]] == [
        "✗ checkout problem",
        "✗ /h/memory is not a directory — converge aborts the whole start on this "
        "root; fix the path (or move it aside) so a real directory can be created",
        "✗ /repo/.venv/bin/python is missing — the interpreter every service session "
        "launches through would fail after the stop; re-run `uv sync`",
    ]
    assert report.stdout[0] == "\n→ start-readiness observations (not blocking):"
