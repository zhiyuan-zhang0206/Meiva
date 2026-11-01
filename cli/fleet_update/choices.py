# meiva:note
# Every choice of a window is the operator's: each is a required flag, a missing or conflicting one
# is named together with every other before any host is touched or log written, and the stop and
# start choices are forwarded unchanged, never checked against the diff. One choice is a set of
# flags of which exactly one is given, so a required mutually exclusive argparse group would name
# only the first missing one.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeGuard

from .plan.release_target import Channel, TargetRequest, parse_release_tag

# meiva:note
# The checkout this module runs from: the operator's code, which lists the services a stop can
# keep or a start can select. It is not the git repository the window updates the hosts to.
CODE_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# A whole, non-negative number of seconds: a negative window is a typo, not a policy.
def _seconds(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of seconds") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds >= 0")
    return value


# meiva:note
# Before any host: names must be in this checkout's roster; shells and browser have own choices.
def _service_problems(args: argparse.Namespace, env: Mapping[str, str]) -> list[str]:
    named = {n for flag in _NAMED for n in getattr(args, flag[2:].replace("-", "_"), None) or ()}
    own = sorted(set(getattr(args, "keep_service", None) or ()) & {"pty-sessions", "browser"})
    problems = [f"--keep-service {name}: it has its own keep/stop choice" for name in own]
    if not named:
        return problems
    with tempfile.TemporaryDirectory() as home:
        roster_env = {**env, "MEIVA_HOME": home, "MEIVA_CONFIG_FETCH": "skip"}
        run = [sys.executable, "-m", "services.supervision.roster"]
        done = subprocess.run(
            run, cwd=CODE_ROOT, env=roster_env, capture_output=True, text=True, check=False
        )
    if done.returncode != 0:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return [
            *problems,
            f"cannot list this checkout's services (exit {done.returncode}): {tail}",
        ]
    unknown = sorted(named - set(done.stdout.split()))
    return problems + ([f"unknown service(s): {', '.join(unknown)}"] if unknown else [])


# meiva:note
# One choice is a set of flags of which exactly one is given, so every missing choice is named at
# once, before any host is touched. A required mutually exclusive group would name only the first.
_PAIRS: dict[str, tuple[str, ...]] = {
    "execution": ("--dry-run", "--execute"),
    "selection": ("--all-services", "--only-service", "--disable-service"),
    "shell": ("--keep-shell-sessions", "--close-shell-sessions"),
    "browser": ("--keep-browser", "--stop-browser"),
    "extras": ("--keep-extras", "--stop-extras"),
    "infra": ("--keep-infra", "--stop-infra"),
    "kept": ("--keep-service", "--keep-no-services"),
    "message": ("--message", "--no-message"),
    "python": ("--allow-python-change", "--refuse-python-change"),
    "red": ("--allow-red-schedules", "--refuse-red-schedules"),
}
# meiva:note
# Every half names the start selection and deadline: `up` and a failed `down`'s recovery use them.
_HALF_PAIRS: dict[str, list[str]] = {
    "both": ["execution", "selection"],
    "down": ["shell", "browser", "extras", "infra", "kept", "message", "python", "red"],
    "up": [],
}
_HALF_VALUES: dict[str, tuple[tuple[str, Callable[[str], object]], ...]] = {
    "both": (("--start-timeout", int),),
    "down": (
        ("--mode", str),
        ("--stop-timeout", int),
        ("--silence-hours", float),
        ("--lease-confirm-seconds", _seconds),
    ),
    "up": (("--smoke-timeout", int), ("--roster-timeout", int)),
}
_NAMED = {"--only-service", "--disable-service", "--keep-service"}
_SCOPES = {"run": ("both", "down", "up"), "down": ("both", "down"), "up": ("both", "up")}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.fleet_update",
        description="Update a networked source-mode cluster over SSH. Every choice is an explicit "
        "flag, forwarded unchanged to each host's `meiva stop` / `meiva start`.",
    )
    halves = parser.add_subparsers(dest="half", required=True)
    for half, help_text in (
        ("run", "complete the update: down, then up and verification"),
        ("down", "stop and switch every host; leave stopped for manual release steps"),
        ("up", "start and verify the cluster, including recovery after a failed up"),
    ):
        p = halves.add_parser(half, help=help_text)
        p.add_argument("--gateway", required=True, metavar="SSH_ALIAS")
        p.add_argument("--runner", action="append", default=None, metavar="SSH_ALIAS")
        p.add_argument("--no-runners", action="store_true", help="the cluster has no runner hosts")
        p.add_argument("--log-dir", type=Path, required=True)
        if half != "up":
            p.add_argument(
                "--channel",
                action="append",
                default=None,
                metavar="CHANNEL",
                help="certified release channel to roll out (`stable` or `latest`)",
            )
            p.add_argument(
                "--release",
                action="append",
                default=None,
                metavar="TAG",
                help="certified CalVer release tag (`vYYYY.MM.DD.HHMMSS[-hfN]`) to roll out",
            )
            p.add_argument(
                "--track-branch",
                action="append",
                default=None,
                metavar="BRANCH",
                help="explicitly track unreleased `origin/<BRANCH>` (preview / rapid iteration)",
            )
            p.add_argument(
                "--new",
                action="append",
                default=None,
                metavar="REV",
                help="certified commit to switch to (or ancestor pin with --track-branch)",
            )
            p.add_argument(
                "--rebuild-extras",
                action="store_true",
                help="accept rebuilding a loaded, stale permissions helper on the hosts that "
                "need it (they retire extras first; each rebuilt helper's desktop grants are "
                "re-applied by hand)",
            )
        for scope in _SCOPES[half]:
            for dest in _HALF_PAIRS[scope]:
                for flag in _PAIRS[dest]:
                    if flag in _NAMED:
                        p.add_argument(flag, action="append", default=None, metavar="NAME")
                    elif flag == "--message":
                        p.add_argument(
                            flag,
                            dest="message_values",
                            action="append",
                            default=None,
                            metavar="TEXT",
                            help="natural-language explanation delivered to live agents on every "
                            "host (who is updating and why)",
                        )
                    else:
                        p.add_argument(flag, dest=dest, action="append_const", const=flag)
            for flag, kind in _HALF_VALUES[scope]:
                p.add_argument(flag, type=kind, default=None)
    return parser


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _value(args: argparse.Namespace, flag: str) -> object:
    return getattr(args, flag[2:].replace("-", "_"))


def _value_problems(args: argparse.Namespace, scope: str) -> list[str]:
    problems: list[str] = []
    for flag, kind in _HALF_VALUES[scope]:
        value = _value(args, flag)
        if value is None or (
            kind in (int, float) and isinstance(value, (int, float)) and value <= 0
        ):
            problems.append(flag if value is None else f"{flag} {value}: not a number > 0")
    return problems


def _resolve_message_choice(args: argparse.Namespace) -> str | None:
    raw_texts: object = getattr(args, "message_values", None)
    texts = raw_texts if _is_object_list(raw_texts) else []
    raw_flags: object = getattr(args, "message", None)
    flag_items = [str(item) for item in raw_flags] if _is_object_list(raw_flags) else []
    given = (["--message"] * len(texts)) + flag_items
    if len(given) != 1:
        return "exactly one of " + " | ".join(_PAIRS["message"])
    if given[0] == "--no-message":
        args.message = "--no-message"
        return None
    stripped = str(texts[0]).strip()
    if not stripped:
        return "--message: cannot be empty; pass --no-message instead"
    args.message = "--message"
    args.message_text = stripped
    return None


def _resolve_pair_choice(args: argparse.Namespace, dest: str) -> str | None:
    if dest == "message":
        return _resolve_message_choice(args)
    given = [f for f in _PAIRS[dest] if f in _NAMED and _value(args, f)]
    given += getattr(args, dest, None) or []
    if len(given) != 1:
        return "exactly one of " + " | ".join(_PAIRS[dest])
    setattr(args, dest, given[0])
    return None


_TARGET_SUMMARY = "--channel stable|latest | --release TAG | --track-branch BRANCH | --new SHA"


def _clean_token(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("-") or any(ch.isspace() for ch in stripped):
        return None
    return stripped


def _resolve_branch_choice(
    raw_branch: str, commits: list[str]
) -> tuple[TargetRequest | None, list[str]]:
    problems: list[str] = []
    branch = _clean_token(raw_branch)
    if branch is None:
        problems.append(f"--track-branch {raw_branch!r}: expected a branch name")
    elif branch.startswith(("origin/", "refs/")):
        problems.append(
            f"--track-branch {branch!r}: pass the branch name without 'origin/' or 'refs/' prefix"
        )
    pin: str | None = None
    if commits:
        pin = _clean_token(commits[0])
        if pin is None:
            problems.append(f"--new {commits[0]!r}: expected a commit SHA or revision")
    if problems or branch is None:
        return None, problems
    return TargetRequest(target_mode="track-branch", branch=branch, commit_rev=pin), []


def _arg_strings(args: argparse.Namespace, name: str) -> list[str]:
    raw: object = getattr(args, name, None)
    return [str(item) for item in raw] if _is_object_list(raw) else []


def _parse_channel_name(raw: str) -> Channel | None:
    stripped = raw.strip()
    if stripped == "stable":
        return "stable"
    if stripped == "latest":
        return "latest"
    return None


def _resolve_single_mode(
    mode: str, values: list[str], commits: list[str]
) -> tuple[TargetRequest | None, list[str]]:
    if mode == "channel":
        channel = _parse_channel_name(values[0])
        if channel is None:
            return None, ["--channel stable|latest"]
        return TargetRequest(target_mode="channel", channel=channel), []
    if mode == "release":
        raw_release = values[0].strip()
        if parse_release_tag(raw_release) is None:
            return None, [
                f"--release {values[0]!r}: expected CalVer tag vYYYY.MM.DD.HHMMSS "
                "or vYYYY.MM.DD.HHMMSS-hfN"
            ]
        return TargetRequest(target_mode="release", release_tag=raw_release), []
    if mode == "branch":
        return _resolve_branch_choice(values[0], commits)
    rev = _clean_token(values[0])
    if rev is None:
        return None, [f"--new {values[0]!r}: expected a commit SHA or revision"]
    return TargetRequest(target_mode="commit", commit_rev=rev), []


# meiva:note
# One target per invocation: the flags name at most one of `--channel`, `--release`,
# `--track-branch` and `--new`. `--track-branch BRANCH --new SHA` is that one mode, the commit
# supplying where the branch starts; every other pair names two targets and is refused.
def _select_target_mode(
    channels: list[str], releases: list[str], branches: list[str], commits: list[str]
) -> tuple[str, list[str]] | None:
    named = [
        (mode, values)
        for mode, values in (
            ("channel", channels),
            ("release", releases),
            ("branch", branches),
            ("commit", commits),
        )
        if values
    ]
    if len(named) == 1:
        return named[0]
    if {mode for mode, _ in named} == {"branch", "commit"}:
        return "branch", branches
    return None


def _resolve_target_choice(args: argparse.Namespace) -> list[str]:
    channels = _arg_strings(args, "channel")
    releases = _arg_strings(args, "release")
    branches = _arg_strings(args, "track_branch")
    commits = _arg_strings(args, "new")
    if not (channels or releases or branches or commits):
        return [_TARGET_SUMMARY]
    if any(len(items) > 1 for items in (channels, releases, branches, commits)):
        return [f"conflicting target flags: choose one of {_TARGET_SUMMARY}"]
    selected = _select_target_mode(channels, releases, branches, commits)
    if selected is None:
        return [
            "conflicting target flags: choose --channel stable|latest, --release TAG, "
            "--new SHA, or --track-branch BRANCH [--new SHA]"
        ]
    mode, values = selected
    req, problems = _resolve_single_mode(mode, values, commits)
    args.target_request = req
    return problems


def _missing_choices(args: argparse.Namespace, env: Mapping[str, str]) -> list[str]:
    problems = [] if bool(args.runner) != args.no_runners else ["--runner ALIAS... | --no-runners"]
    args.message_text = None
    args.target_request = None
    if args.half != "up":
        problems += _resolve_target_choice(args)
    for scope in _SCOPES[args.half]:
        for dest in _HALF_PAIRS[scope]:
            if problem := _resolve_pair_choice(args, dest):
                problems.append(problem)
        problems += _value_problems(args, scope)
    if args.half != "up" and args.mode not in (None, "smooth", "force"):
        problems.append("--mode smooth|force")
    return problems + _service_problems(args, env)


def forwarded_flags(args: argparse.Namespace, chosen: str) -> list[str]:
    if chosen in _NAMED:
        values = _value(args, chosen)
        items = values if _is_object_list(values) else []
        return [f"{chosen} {shlex.quote(str(name))}" for name in items]
    return [chosen]


def message_flag(args: argparse.Namespace) -> str:
    if args.message_text is not None:
        return f"--message {shlex.quote(args.message_text)}"
    return "--no-message"


def start_flags(args: argparse.Namespace) -> str:
    return " ".join(forwarded_flags(args, args.selection))


# meiva:note
# Parse and validate the whole argv. A refusal exits 2 with every missing or conflicting choice
# listed at once, before any host is touched.
def parse(argv: list[str], env: Mapping[str, str]) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if problems := _missing_choices(args, env):
        listed = "".join(f"\n  {problem}" for problem in problems)
        parser.exit(2, f"refused: every choice is explicit; missing or conflicting:{listed}\n")
    if getattr(args, "rebuild_extras", False) and getattr(args, "extras", None) == "--keep-extras":
        parser.exit(
            2,
            "refused: --rebuild-extras retires and rebuilds the helper; it excludes "
            "--keep-extras\n",
        )
    args.runner = args.runner or []
    args.dry_run = args.execution == "--dry-run"
    return args
