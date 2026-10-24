# meiva:note
# What the `$MEIVA_HOME` ownership preflight tells the operator.
#
# Pure text over the owners the shell stat-ed: which key paths belong to another user, the
# repair command for each, and the banner and log lines that report them.

from __future__ import annotations

import shlex
from dataclasses import dataclass

__all__ = ["Account", "OwnedPath", "log_lines", "ownership_warnings", "report_lines"]


@dataclass(frozen=True)
class OwnedPath:
    path: str
    owner_uid: int


# meiva:note
# The user converge runs as: its uid and the names `chown` takes.
@dataclass(frozen=True)
class Account:
    uid: int
    user: str
    group: str


# meiva:note
# One warning per path another user owns, each carrying its repair command.
def ownership_warnings(paths: tuple[OwnedPath, ...], account: Account) -> tuple[str, ...]:
    return tuple(
        f"{owned.path}: owned by uid {owned.owner_uid}, current uid {account.uid}; "
        f"repair with: {_repair_command(owned.path, account)}"
        for owned in paths
        if owned.owner_uid != account.uid
    )


def _repair_command(path: str, account: Account) -> str:
    return (
        f"sudo chown -R {shlex.quote(account.user)}:{shlex.quote(account.group)} "
        f"{shlex.quote(path)}"
    )


# meiva:note
# The stderr banner for a non-empty warning set; empty when there is nothing to report.
def report_lines(warnings: tuple[str, ...]) -> tuple[str, ...]:
    if not warnings:
        return ()
    return (
        "\n⚠  OWNERSHIP PREFLIGHT — $MEIVA_HOME paths belong to another user (start continues):",
        *(f"    {warning}" for warning in warnings),
        "    Repair these paths before the next start; this warning never blocks converge.",
    )


# meiva:note
# The lines appended to the preflight log, each stamped.
def log_lines(stamp: str, warnings: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{stamp} {warning}" for warning in warnings)
