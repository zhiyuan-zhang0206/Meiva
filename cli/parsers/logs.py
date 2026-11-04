"""`meiva logs` local log-maintenance parser."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.options import options


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("days must be a positive integer") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("days must be a positive integer")
    return days


def _positive_mib(value: str) -> int:
    try:
        size_mib = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("size MiB must be a positive integer") from exc
    if size_mib <= 0:
        raise argparse.ArgumentTypeError("size MiB must be a positive integer")
    return size_mib


def _family_days(value: str) -> dict[str, int]:
    from cli.commands.observability.logs import parse_family_days

    try:
        return parse_family_days(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


@dataclass(frozen=True)
class _RetentionOptions:
    older_than: int | None
    family_days: Mapping[str, int] | None
    dry_run: bool


def _h_logs_retention(args: argparse.Namespace) -> int:
    from base.paths import logs_dir
    from cli.commands.observability.logs import cmd_logs_retention
    from cli.database.runtime import runtime_of

    opts = options(args, _RetentionOptions)
    runtime = runtime_of(args)
    explicit = opts.older_than is not None or opts.family_days is not None
    return cmd_logs_retention(
        older_than_days=opts.older_than,
        family_days=opts.family_days,
        default_retention_days=(
            None if explicit else runtime.config.view.observability.log_retention_days
        ),
        dry_run=opts.dry_run,
        logs_path=logs_dir(home=runtime.home),
        now=runtime.clock.now(),
    )


@dataclass(frozen=True)
class _RotateOptions:
    dry_run: bool
    size_mib: int
    logs_path: Path | None


def _h_logs_rotate(args: argparse.Namespace) -> int:
    from base.paths import logs_dir
    from cli.commands.observability.logs import cmd_logs_rotate
    from cli.database.runtime import runtime_of

    opts = options(args, _RotateOptions)
    runtime = runtime_of(args)
    return cmd_logs_rotate(
        dry_run=opts.dry_run,
        size_mib=opts.size_mib,
        logs_path=logs_dir(home=runtime.home) if opts.logs_path is None else opts.logs_path,
        now=runtime.clock.now(),
    )


def _h_logs_maintenance(args: argparse.Namespace) -> int:
    from base.paths import logs_dir
    from cli.commands.observability.logs import cmd_logs_maintenance
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    return cmd_logs_maintenance(
        family_days_text=runtime.config.view.observability.log_family_days,
        logs_path=logs_dir(home=runtime.home),
        now=runtime.clock.now(),
    )


def add_logs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    logs_p = sub.add_parser("logs", help="manage local log files")
    logs_sub = logs_p.add_subparsers(dest="logs_cmd", required=True)
    retention_p = logs_sub.add_parser(
        "retention",
        help="delete expired service and native log archives",
        description=(
            "Delete expired managed files from the top level of $MEIVA_HOME/logs, "
            "$MEIVA_HOME/lgtm/native/logs, and the nested computer-use snapshot "
            "dir while preserving active open files."
        ),
    )
    age_mode = retention_p.add_mutually_exclusive_group()
    age_mode.add_argument(
        "--older-than",
        type=_positive_days,
        default=None,
        metavar="DAYS",
        help=(
            "positive age threshold in days (default: MEIVA_LOG_RETENTION_DAYS, otherwise 14 days)"
        ),
    )
    age_mode.add_argument(
        "--family-days",
        type=_family_days,
        default=None,
        metavar="FAMILY=DAYS,...",
        help=(
            "comma-separated retention overrides; mutually exclusive with --older-than. "
            "Tier defaults: agent=15, shell=7, gateway=30, ops=30, watchdog=30, "
            "snapshot=7, other=15 days (the no-flag fallback remains "
            "MEIVA_LOG_RETENTION_DAYS, otherwise 14 days)"
        ),
    )
    retention_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list matching expired files and totals without deleting",
    )
    retention_p.set_defaults(func=_h_logs_retention)
    maintenance_p = logs_sub.add_parser(
        "maintenance",
        help="rotate then retain, using the configured retention policy",
        description=(
            "The daily job's entry point: rotate every managed log, then delete expired "
            "files using MEIVA_LOG_FAMILY_DAYS. The job command names this behaviour and "
            "carries no policy of its own, so a release cannot rewrite the operator's "
            "retention window."
        ),
    )
    maintenance_p.set_defaults(func=_h_logs_maintenance)

    rotate_p = logs_sub.add_parser(
        "rotate",
        help="copytruncate active service and native backend logs",
        description=(
            "Copytruncate top-level service stdout and native backend logs when "
            "they reach the size threshold or cross a UTC day boundary."
        ),
    )
    rotate_p.add_argument(
        "--dry-run",
        action="store_true",
        help="report which files would rotate without writing",
    )
    # task #4092 cli-default inventory: rotation threshold — parse-validated
    # positive at the boundary; the default is the standing log-growth contract.
    rotate_p.add_argument(
        "--size-mib",
        type=_positive_mib,
        # meiva:note
        # The literal stays here on purpose. `cli.parsers` must build the whole argument
        # tree without importing settings, and reaching the command module for its
        # DEFAULT_ROTATE_SIZE_MIB would do exactly that (test_command_import_boundary...).
        # The two values are pinned together by the command module's own default.
        default=64,
        metavar="N",
        help="rotation size threshold in MiB (default: 64)",
    )
    rotate_p.add_argument(
        "--logs-path",
        type=Path,
        default=None,
        metavar="PATH",
        help="service log directory (default: $MEIVA_HOME/logs)",
    )
    rotate_p.set_defaults(func=_h_logs_rotate)
