"""Warning-only ownership checks for files converge must be able to update."""

from __future__ import annotations

import grp
import pwd
import sys
from datetime import datetime
from pathlib import Path

from cli.commands.converge.plans.ownership_plan import (
    Account,
    OwnedPath,
    log_lines,
    ownership_warnings,
    report_lines,
)
from cli.commands.converge.spec import ConvergeCtx


# meiva:note
# Non-user-owned key $MEIVA_HOME paths, as warnings with their repair commands.
def collect_ownership_warnings(ctx: ConvergeCtx) -> tuple[str, ...]:
    owned = tuple(OwnedPath(str(path), path.stat().st_uid) for path in _key_paths(ctx.meiva_home))
    return ownership_warnings(owned, _account(ctx.host.uid))


def ensure_ownership_preflight(ctx: ConvergeCtx) -> None:
    """Print and log ownership repairs while always allowing converge to proceed."""
    try:
        warnings = collect_ownership_warnings(ctx)
    except Exception as exc:
        print(f"  · ownership preflight skipped: {exc}", file=sys.stderr)
        return
    for line in report_lines(warnings):
        print(line, file=sys.stderr)
    if warnings:
        _append_warnings(
            ctx.meiva_home / "logs" / "ownership_preflight.log", warnings, now=ctx.clock.now()
        )


def _key_paths(meiva_home: Path) -> tuple[Path, ...]:
    paths = (
        meiva_home,
        meiva_home / ".env",
        meiva_home / "logs",
        meiva_home / "configs",
        meiva_home / "secrets",
        meiva_home / "source",
    )
    return tuple(path for path in paths if path.exists())


# meiva:note
# The names `chown` takes for `uid`, or the numeric ids when the system has no entry.
def _account(uid: int) -> Account:
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return Account(uid, str(uid), str(uid))
    gid = pw.pw_gid
    try:
        return Account(uid, pw.pw_name, grp.getgrgid(gid).gr_name)
    except KeyError:
        return Account(uid, pw.pw_name, str(gid))


def _append_warnings(path: Path, warnings: tuple[str, ...], *, now: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = now.isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as output:
            for line in log_lines(stamp, warnings):
                output.write(f"{line}\n")
    except OSError as exc:
        # A root-owned logs directory is the incident this preflight reveals;
        # failure to record there must not hide the console repair command.
        print(f"  · ownership preflight log unavailable: {exc}", file=sys.stderr)
