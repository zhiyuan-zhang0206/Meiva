# meiva:note
# The `$MEIVA_HOME` ownership preflight: what it reports, and that it never blocks converge.

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from base.host.system.tests.fake_commands import fake_host
from cli.commands.converge import ownership_preflight as _ownership
from cli.commands.converge.plans.ownership_plan import (
    Account,
    OwnedPath,
    log_lines,
    ownership_warnings,
    report_lines,
)
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host

_ME = Account(uid=501, user="meiva", group="staff")


# meiva:note
# --- what is reported (pure) --------------------------------------------------------


def test_warnings_name_only_the_paths_another_user_owns_with_their_repair() -> None:
    paths = (
        OwnedPath("/home/meiva", 501),
        OwnedPath("/home/meiva/source", 0),
        OwnedPath("/home/meiva/logs", 501),
    )

    assert ownership_warnings(paths, _ME) == (
        "/home/meiva/source: owned by uid 0, current uid 501; repair with: "
        "sudo chown -R meiva:staff /home/meiva/source",
    )


def test_the_repair_command_quotes_what_the_shell_would_split() -> None:
    warnings = ownership_warnings(
        (OwnedPath("/home/my meiva/source", 0),), Account(501, "john doe", "staff")
    )

    assert warnings[0].endswith("sudo chown -R 'john doe':staff '/home/my meiva/source'")


def test_nothing_foreign_means_nothing_to_report() -> None:
    assert ownership_warnings((OwnedPath("/home/meiva", 501),), _ME) == ()
    assert report_lines(()) == ()


def test_the_banner_lists_every_warning_between_the_title_and_the_reassurance() -> None:
    lines = report_lines(("first", "second"))

    assert lines[0].endswith("paths belong to another user (start continues):")
    assert lines[1:3] == ("    first", "    second")
    assert (
        lines[3]
        == "    Repair these paths before the next start; this warning never blocks converge."
    )


def test_log_lines_carry_the_stamp() -> None:
    assert log_lines("2026-10-24T00:00:00+00:00", ("a", "b")) == (
        "2026-10-24T00:00:00+00:00 a",
        "2026-10-24T00:00:00+00:00 b",
    )


# meiva:note
# --- against a real home ------------------------------------------------------------


# meiva:note
# A context whose home holds every key path, converged by a user with uid `as_uid`.
def _home_with_key_paths(ctx: ConvergeCtx, *, as_uid: int) -> ConvergeCtx:
    ctx.meiva_home.mkdir()
    for name in (".env", "logs", "configs", "secrets", "source"):
        path = ctx.meiva_home / name
        if name == ".env":
            path.touch()
        else:
            path.mkdir()
    return replace(ctx, host=fake_host(uid=as_uid))


def test_the_owner_of_every_key_path_has_nothing_to_repair(ctx: ConvergeCtx) -> None:
    mine = _home_with_key_paths(ctx, as_uid=os.getuid())

    assert _ownership.collect_ownership_warnings(mine) == ()


def test_every_key_path_is_foreign_to_another_user(ctx: ConvergeCtx) -> None:
    other = os.getuid() + 1
    foreign = _home_with_key_paths(ctx, as_uid=other)

    warnings = _ownership.collect_ownership_warnings(foreign)

    assert [warning.split(":")[0] for warning in warnings] == [
        str(ctx.meiva_home),
        str(ctx.meiva_home / ".env"),
        str(ctx.meiva_home / "logs"),
        str(ctx.meiva_home / "configs"),
        str(ctx.meiva_home / "secrets"),
        str(ctx.meiva_home / "source"),
    ]
    assert all(f"owned by uid {os.getuid()}, current uid {other}" in w for w in warnings)


def test_a_missing_source_tree_is_skipped(ctx: ConvergeCtx) -> None:
    other = os.getuid() + 1
    foreign = _home_with_key_paths(ctx, as_uid=other)
    (ctx.meiva_home / "source").rmdir()

    warnings = _ownership.collect_ownership_warnings(foreign)

    assert not any(str(ctx.meiva_home / "source") in warning for warning in warnings)


def test_ownership_preflight_prints_and_logs_without_blocking(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = _home_with_key_paths(ctx, as_uid=os.getuid() + 1)

    # meiva:note
    # must not raise
    _ownership.ensure_ownership_preflight(foreign)

    err = capsys.readouterr().err
    assert "OWNERSHIP PREFLIGHT" in err
    assert "start continues" in err
    assert f"{ctx.meiva_home / 'source'}: owned by uid" in err
    log = (ctx.meiva_home / "logs" / "ownership_preflight.log").read_text()
    assert f"{ctx.meiva_home / 'source'}: owned by uid" in log


def test_a_clean_home_prints_and_logs_nothing(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = _home_with_key_paths(ctx, as_uid=os.getuid())

    _ownership.ensure_ownership_preflight(mine)

    assert capsys.readouterr().err == ""
    assert not (ctx.meiva_home / "logs" / "ownership_preflight.log").exists()


def test_an_unwritable_log_does_not_hide_the_repair_command(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = _home_with_key_paths(ctx, as_uid=os.getuid() + 1)
    (ctx.meiva_home / "logs").rmdir()
    (ctx.meiva_home / "logs").write_text("a file where the log directory belongs")

    _ownership.ensure_ownership_preflight(foreign)

    err = capsys.readouterr().err
    assert "repair with: sudo chown -R" in err
    assert "ownership preflight log unavailable" in err


# meiva:note
# A home whose path the filesystem cannot even stat fails the scan, not converge.
def test_ownership_preflight_never_fails_converge_when_its_scan_errors(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx.meiva_home.mkdir()
    # meiva:note
    # a name too long to stat
    unscannable = replace(ctx, meiva_home=ctx.meiva_home / ("x" * 300))

    _ownership.ensure_ownership_preflight(unscannable)

    assert "ownership preflight skipped:" in capsys.readouterr().err


def test_ownership_preflight_is_a_file_only_converge_step() -> None:
    step = converge_host.CONVERGE_STEPS[0]

    assert step.name == "$MEIVA_HOME ownership preflight"
    assert step.apply is _ownership.ensure_ownership_preflight
    assert step.requires_unit_config is False
