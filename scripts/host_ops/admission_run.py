#!/usr/bin/env python3
# meiva:note
# Simplified passthrough runner for host jobs with CLI compatibility for historical
# admission flags.
# meiva:link scripts/host_ops/docs/admission-run/admission-run.meiva.okf.md
from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


def admission_home() -> Path:
    return Path.home() / ".meiva" / "admission"


INTERPRETERS = frozenset({"uv", "node", "bash", "sh", "zsh", "env"})


def job_key(command: Sequence[str]) -> str:
    if not command:
        return ""
    first = Path(command[0]).name
    if first.startswith("python") or first in INTERPRETERS:
        for token in command[1:]:
            if token.endswith(".py"):
                return Path(token).name
        for token in command[1:]:
            if not token.startswith("-"):
                return Path(token).name
    return first


def requested_jobs(command: Sequence[str]) -> tuple[int, int | None]:
    for idx, arg in enumerate(command):
        if arg in ("--jobs", "--eval-jobs") and idx + 1 < len(command):
            val = command[idx + 1]
            if val.isdigit():
                return int(val), idx + 1
        if arg.startswith(("--jobs=", "--eval-jobs=")):
            val = arg.split("=", 1)[1]
            if val.isdigit():
                return int(val), idx
        if arg.startswith("-j") and len(arg) > 2:
            val = arg[2:]
            if val.isdigit():
                return int(val), idx
        if arg == "-j" and idx + 1 < len(command):
            val = command[idx + 1]
            if val.isdigit():
                return int(val), idx + 1
    return 1, None


@dataclass(frozen=True)
class ParsedArgs:
    dry_run: bool = False
    jobs: int | None = None


def parse_args(argv: Sequence[str]) -> tuple[ParsedArgs, list[str]]:
    parser = argparse.ArgumentParser(
        description="Host job runner with backward-compatible admission flags",
        add_help=False,
    )
    _ = parser.add_argument("--peak-mb", type=float, default=None)
    _ = parser.add_argument("--jobs", type=int, default=None)
    _ = parser.add_argument("--reserve-gib", type=float, default=12.0)
    _ = parser.add_argument("--swap-multiplier", type=float, default=2.0)
    _ = parser.add_argument("--no-admission", type=str, default=None)
    _ = parser.add_argument("--max-wait", type=float, default=1800.0)
    _ = parser.add_argument("--no-wait", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--home", type=Path, default=None)
    _ = parser.add_argument("--disk-floor-gib", type=float, default=23.0)
    _ = parser.add_argument("--disk-warn-gib", type=float, default=26.0)
    _ = parser.add_argument("--disk-reset-margin-gib", type=float, default=1.5)
    _ = parser.add_argument("--disk-samples", type=int, default=3)
    _ = parser.add_argument("--disk-sample-interval", type=float, default=10.0)

    if "--" in argv:
        split_idx = argv.index("--")
        opt_args = list(argv[:split_idx])
        cmd = list(argv[split_idx + 1 :])
    else:
        parsed, remaining = parser.parse_known_args(argv)
        args = ParsedArgs(
            dry_run=bool(getattr(parsed, "dry_run", False)),
            jobs=getattr(parsed, "jobs", None),
        )
        return args, remaining

    parsed, remaining = parser.parse_known_args(opt_args)
    args = ParsedArgs(
        dry_run=bool(getattr(parsed, "dry_run", False)),
        jobs=getattr(parsed, "jobs", None),
    )
    return args, (remaining + cmd)


# meiva:note
# Host command runner entrypoint; executes the forwarded command directly.
def main(
    argv: Sequence[str] | None = None,
    *,
    executor: Callable[..., int] | None = None,
    **_kwargs: object,
) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args, command = parse_args(argv)
    if args.dry_run:
        named, _ = requested_jobs(command)
        jobs = args.jobs if args.jobs is not None else named
        print(f"admission: dry run — would start {jobs} of {jobs} workers", flush=True)
        return 0
    if not command:
        return 0
    if executor is not None:
        try:
            return executor(command, {"jobs": args.jobs, "command": command})
        except TypeError:
            return executor(command)
    return subprocess.run(command, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    sys.exit(main())
