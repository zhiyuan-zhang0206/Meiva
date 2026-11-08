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
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from .plan.release_target import DEFAULT_CHANNEL, Channel, TargetRequest
from .untyped import object_of

# meiva:note
# The checkout this module runs from: the operator's code, which lists the services a stop can
# keep or a start can select. It is not the git repository the window updates the hosts to.
CODE_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# Before any host: names must be in this checkout's roster; shells and browser have own choices.
# The roster is this checkout's own module, run by the interpreter the entry point named.
def _service_problems(
    args: argparse.Namespace, env: Mapping[str, str], *, python: str
) -> list[str]:
    named = {n for flag in _NAMED for n in _strings(args, flag[2:].replace("-", "_"))}
    own = sorted(set(_strings(args, "keep_service")) & {"pty-sessions", "browser"})
    problems = [f"--keep-service {name}: it has its own keep/stop choice" for name in own]
    if not named:
        return problems
    with tempfile.TemporaryDirectory() as home:
        roster_env = {**env, "MEIVA_HOME": home, "MEIVA_CONFIG_FETCH": "skip"}
        run = [python, "-m", "services.supervision.roster"]
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
    ),
    "up": (("--roster-timeout", int),),
}
_NAMED = {"--only-service", "--disable-service", "--keep-service"}
_SCOPES = {"run": ("both", "down", "up"), "down": ("both", "down"), "up": ("both", "up")}


UPDATE_DESCRIPTION = (
    "Update a networked source-mode cluster over SSH. Every choice is an explicit "
    "flag, forwarded unchanged to each host's `meiva stop` / `meiva start`."
)


def populate_update_parser(
    parser: argparse.ArgumentParser,
    *,
    handler: Callable[[argparse.Namespace], int] | None,
) -> None:
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
                help="certified release channel to roll out (`stable` or `latest`; "
                "defaults to `latest` when `--track-branch` is not set)",
            )
            p.add_argument(
                "--track-branch",
                action="append",
                default=None,
                metavar="BRANCH",
                help="explicitly track the latest commit on unreleased `origin/<BRANCH>` "
                "(preview / rapid iteration)",
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
        if handler is not None:
            p.set_defaults(func=handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meiva update",
        description=UPDATE_DESCRIPTION,
    )
    populate_update_parser(parser, handler=None)
    return parser


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# The attribute `name` of the parsed namespace as an `object` (`None` when argparse never made it).
def _attr(args: argparse.Namespace, name: str) -> object:
    return object_of(vars(args).get, name)


# meiva:note
# The attribute `name` as a list of strings: a repeated flag's values, empty when unset.
def _strings(args: argparse.Namespace, name: str) -> list[str]:
    raw = _attr(args, name)
    return [str(item) for item in raw] if _is_object_list(raw) else []


def _value(args: argparse.Namespace, flag: str) -> object:
    return _attr(args, flag[2:].replace("-", "_"))


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
    raw_texts = _attr(args, "message_values")
    texts = raw_texts if _is_object_list(raw_texts) else []
    flag_items = _strings(args, "message")
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
    given += _strings(args, dest)
    if len(given) != 1:
        return "exactly one of " + " | ".join(_PAIRS[dest])
    vars(args)[dest] = given[0]
    return None


_TARGET_SUMMARY = "--channel stable|latest | --track-branch BRANCH"


def _clean_token(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("-") or any(ch.isspace() for ch in stripped):
        return None
    return stripped


def _resolve_branch_choice(raw_branch: str) -> tuple[TargetRequest | None, list[str]]:
    branch = _clean_token(raw_branch)
    if branch is None:
        return None, [f"--track-branch {raw_branch!r}: expected a branch name"]
    if branch.startswith(("origin/", "refs/")):
        return None, [
            f"--track-branch {branch!r}: pass the branch name without 'origin/' or 'refs/' prefix"
        ]
    return TargetRequest(target_mode="track-branch", branch=branch), []


def _parse_channel_name(raw: str) -> Channel | None:
    stripped = raw.strip()
    if stripped == "stable":
        return "stable"
    if stripped == "latest":
        return "latest"
    return None


# meiva:note
# One target per invocation: either a certified channel (`--channel stable|latest`, defaulting to
# `latest` when neither flag is given) or an unreleased branch (`--track-branch BRANCH`).
def _resolve_target_choice(args: argparse.Namespace) -> list[str]:
    channels = _strings(args, "channel")
    branches = _strings(args, "track_branch")
    if not channels and not branches:
        args.target_request = TargetRequest(target_mode="channel", channel=DEFAULT_CHANNEL)
        return []
    if len(channels) > 1 or len(branches) > 1 or (channels and branches):
        return [f"conflicting target flags: choose {_TARGET_SUMMARY}"]
    if channels:
        channel = _parse_channel_name(channels[0])
        if channel is None:
            return ["--channel stable|latest"]
        args.target_request = TargetRequest(target_mode="channel", channel=channel)
        return []
    req, problems = _resolve_branch_choice(branches[0])
    args.target_request = req
    return problems


def _missing_choices(args: argparse.Namespace, env: Mapping[str, str], *, python: str) -> list[str]:
    half = _text(args, "half")
    has_runners = bool(_strings(args, "runner"))
    problems = (
        []
        if has_runners != (_attr(args, "no_runners") is True)
        else ["--runner ALIAS... | --no-runners"]
    )
    args.message_text = None
    args.target_request = None
    if half != "up":
        problems += _resolve_target_choice(args)
    for scope in _SCOPES[half]:
        for dest in _HALF_PAIRS[scope]:
            if problem := _resolve_pair_choice(args, dest):
                problems.append(problem)
        problems += _value_problems(args, scope)
    if half != "up" and _attr(args, "mode") not in (None, "smooth", "force"):
        problems.append("--mode smooth|force")
    return problems + _service_problems(args, env, python=python)


# meiva:note
# The choices of the `down` half, as the operator made them: each flag of a pair is the one
# chosen (`--keep-infra`), each value is the validated number or text, and `kept` is the
# `--keep-service`/`--keep-no-services` choice whose named services travel in `FleetArgs.named`.
@dataclass(frozen=True)
class DownChoices:
    shell: str
    browser: str
    extras: str
    infra: str
    kept: str
    mode: str
    stop_timeout: int
    silence_hours: float
    python: str
    red: str


# meiva:note
# Everything a window reads from its command line, validated and typed. `down` is present for the
# `run` and `down` halves, `roster_timeout` for `run` and `up`. `named` maps each repeated service
# flag (`--only-service`, `--disable-service`, `--keep-service`) to the names it was given.
@dataclass(frozen=True)
class FleetArgs:
    half: str
    gateway: str
    runner: tuple[str, ...]
    log_dir: Path
    dry_run: bool
    selection: str
    named: Mapping[str, tuple[str, ...]]
    start_timeout: int
    roster_timeout: int | None
    target_request: TargetRequest | None
    message_text: str | None
    rebuild_extras: bool
    down: DownChoices | None


def forwarded_flags(args: FleetArgs, chosen: str) -> list[str]:
    if chosen in _NAMED:
        return [f"{chosen} {shlex.quote(name)}" for name in args.named[chosen]]
    return [chosen]


def message_flag(args: FleetArgs) -> str:
    if args.message_text is not None:
        return f"--message {shlex.quote(args.message_text)}"
    return "--no-message"


def start_flags(args: FleetArgs) -> str:
    return " ".join(forwarded_flags(args, args.selection))


def _text(args: argparse.Namespace, name: str) -> str:
    value = _attr(args, name)
    if not isinstance(value, str):
        raise TypeError(f"--{name.replace('_', '-')} is {type(value).__name__}, not text")
    return value


def _whole(args: argparse.Namespace, name: str) -> int:
    value = _attr(args, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"--{name.replace('_', '-')} is {type(value).__name__}, not a whole number")
    return value


def _optional_whole(args: argparse.Namespace, name: str) -> int | None:
    return None if _attr(args, name) is None else _whole(args, name)


def _real(args: argparse.Namespace, name: str) -> float:
    value = _attr(args, name)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"--{name.replace('_', '-')} is {type(value).__name__}, not a number")
    return float(value)


def _down_choices(args: argparse.Namespace) -> DownChoices:
    return DownChoices(
        shell=_text(args, "shell"),
        browser=_text(args, "browser"),
        extras=_text(args, "extras"),
        infra=_text(args, "infra"),
        kept=_text(args, "kept"),
        mode=_text(args, "mode"),
        stop_timeout=_whole(args, "stop_timeout"),
        silence_hours=_real(args, "silence_hours"),
        python=_text(args, "python"),
        red=_text(args, "red"),
    )


def _fleet_args(args: argparse.Namespace) -> FleetArgs:
    target = _attr(args, "target_request")
    message = _attr(args, "message_text")
    log_dir = _attr(args, "log_dir")
    if not isinstance(log_dir, Path):
        raise TypeError("--log-dir is not a path")
    return FleetArgs(
        half=_text(args, "half"),
        gateway=_text(args, "gateway"),
        runner=tuple(_strings(args, "runner")),
        log_dir=log_dir,
        dry_run=_attr(args, "execution") == "--dry-run",
        selection=_text(args, "selection"),
        named={flag: tuple(_strings(args, flag[2:].replace("-", "_"))) for flag in _NAMED},
        start_timeout=_whole(args, "start_timeout"),
        roster_timeout=_optional_whole(args, "roster_timeout"),
        target_request=target if isinstance(target, TargetRequest) else None,
        message_text=message if isinstance(message, str) else None,
        rebuild_extras=_attr(args, "rebuild_extras") is True,
        down=None if _text(args, "half") == "up" else _down_choices(args),
    )


# meiva:note
# Validate a parsed `meiva update` namespace: return its `FleetArgs`, or the refusal text naming
# every missing or conflicting choice at once before any host is touched.
def validate_parsed(
    args: argparse.Namespace, env: Mapping[str, str], *, python: str
) -> FleetArgs | str:
    if problems := _missing_choices(args, env, python=python):
        listed = "".join(f"\n  {problem}" for problem in problems)
        return f"refused: every choice is explicit; missing or conflicting:{listed}\n"
    if _attr(args, "rebuild_extras") is True and _attr(args, "extras") == "--keep-extras":
        return (
            "refused: --rebuild-extras retires and rebuilds the helper; it excludes --keep-extras\n"
        )
    return _fleet_args(args)


# meiva:note
# Parse and validate the whole argv. A refusal exits 2 with every missing or conflicting choice
# listed at once, before any host is touched.
def parse(argv: list[str], env: Mapping[str, str], *, python: str) -> FleetArgs:
    parser = _parser()
    args = parser.parse_args(argv)
    outcome = validate_parsed(args, env, python=python)
    if isinstance(outcome, str):
        parser.exit(2, outcome)
    return outcome
