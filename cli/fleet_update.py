# meiva:note
# cli.fleet_update imports no application code — no base, cli, gateway, services, agent or meiva modules — so the halves run from a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from subprocess import PIPE, STDOUT
from typing import Any

REPO = Path(__file__).resolve().parents[1]
HOME = 'H="$HOME/.meiva"; S="$H/source"; cd "$S" || exit 1'
MEIVA = 'MEIVA_HOME="$H" "$S/.venv/bin/meiva"'
_PYTHON = f'{HOME}; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python"'
_STATUS = f"{HOME}; {MEIVA} status --json"
_FREE = ("inactive", "resumed")
POLL_S = 5

# meiva:note
# Five lines a minute: enough for an operator to watch the window move, few enough not to bury the
# leases named above it.
LEASE_TICK_S = 5.0
_STOP_HINT = (
    "\nOn that host retry `meiva stop` with the same arguments this `down` gave it (the logged "
    "command above), confirm the maintenance hold is paused/stopped (`meiva status`), then "
    "rerun `down`."
)
CLEAN = (
    'd=$(git status --porcelain | head -n 20); [ -z "$d" ] || { echo "$d"; echo "Untracked files '
    "are not caused by this update (a changed .gitignore can reveal them): check them, then "
    'move them away (do not delete) and rerun down."; exit 1; }'
)
PROBE = f"""{HOME}
printf '%s\\n' "head=$(git rev-parse HEAD)"
printf '%s\\n' "dirty=$(git status --porcelain 2>&1 | wc -l | tr -d ' ')"
test -e "$(git rev-parse --git-path hooks/post-checkout)" && echo hook=yes || echo hook=no
test -e "$H/updates/active" && echo active=yes || echo active=no
printf '%s\\n' "hold=$({MEIVA} status --json 2>/dev/null | grep '^{{' | tail -n 1)\""""
_VENV_DIRS = (
    "find .venv/bin .venv/lib -maxdepth 3 -type d \\( -path .venv/bin "
    '-o -path ".venv/lib/python*/site-packages" '
    '-o -path ".venv/lib/python*/site-packages/meiva-*.dist-info" \\) -exec chmod u+w {} +'
)
_LOCKS = ("uv.lock", "ui/web/package-lock.json", ".python-version")
_WATCHED = ("migrations/", "services/desktop/permissions_helper/helper", *_LOCKS)
_SECRET_NAME = r"\w*(?:secret|password|token|api_key|capability_key)\w*"
_SECRETS = (
    (re.compile(r"(?i)(\bbearer\s+)[^\s\"',;]+"), r"\1<redacted>"),
    (re.compile(rf"(?i)(\b{_SECRET_NAME}[\"']?\s*[=:]\s*[\"']?)[^\s\"',;}}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]*:)[^@\s/]+@"), r"\1<redacted>@"),
)


def gateway_program() -> str:
    return (Path(__file__).parent / "commands" / "management" / "fleet_gateway.py").read_text(
        encoding="utf-8"
    )


VERIFY_SCRIPTS = Path(__file__).parent / "commands" / "management"


def read_rows_py() -> str:
    return (VERIFY_SCRIPTS / "schedules_verify_read_rows.py").read_text(encoding="utf-8")


def check_rows_py() -> str:
    return (VERIFY_SCRIPTS / "schedules_verify_check_rows.py").read_text(encoding="utf-8")


PRE_VERIFY = """{home}
W="$H/pre-update-verify"; R="$W.rows"; T=""
trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -f "$R"; [ -z "$T" ] || rm -rf "$T"' EXIT
git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -rf "$W"; git -C "$S" worktree prune
git cat-file -e "{new}^{{commit}}" || {{ echo "SKIPPED: {new} is not fetched on this host (a dry run does not fetch)"; exit 0; }}
(umask 077; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {read_rows} "$R") \\
  || {{ echo "VERIFY_RC=2"; exit 0; }}
git -C "$S" worktree add -q "$W" {new} || {{ echo "VERIFY_RC=2"; exit 0; }}
T=$(mktemp -d) || {{ echo "VERIFY_RC=2"; exit 0; }}
cd "$W" && MEIVA_HOME="$T" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {check_rows} "$R"
echo "VERIFY_RC=$?"; exit 0"""


class FailedError(RuntimeError):
    pass


def ssh(alias: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", alias, command]
    pipe = PIPE if stdin is not None else subprocess.DEVNULL
    process = subprocess.Popen(argv, stdin=pipe, stdout=PIPE, stderr=STDOUT, text=True)
    if stdin is not None and process.stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    for line in process.stdout or ():
        emit(line.rstrip("\n"))
    return process.wait()


def split_hold_text(text: str) -> tuple[str, str | None]:
    data = json.loads([line for line in text.splitlines() if line.startswith("{")][-1])
    hold = data["hold"]
    held = hold["maintenance"]
    return (hold["status"], held["phase"]) if held else (hold["status"], None)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(REPO), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


class Session:
    def __init__(self, args: argparse.Namespace) -> None:
        self.dry_run: bool = args.dry_run
        args.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = args.log_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}-{args.half}.log"
        self.log = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.say(f"log: {path}")
        self.os: dict[str, str] = {}

    def say(self, line: str) -> None:
        for pattern, replacement in _SECRETS:
            line = pattern.sub(replacement, line)
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def run(self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None) -> str:
        self.say(f"[{alias}] {'$' if effect else 'read:'} " + script.replace("\n", "\n    "))
        if effect and self.dry_run:
            return ""
        if alias in self.os:
            script = f"{'zsh' if self.os[alias] == 'Darwin' else 'bash'} -lc {shlex.quote(script)}"
        lines: list[str] = []

        def emit(line: str) -> None:
            lines.append(line)
            self.say(f"    {alias}| {line}")

        if rc := ssh(alias, script, stdin, emit):
            raise FailedError(f"{alias}: exit {rc}: {lines[-1] if lines else 'no output'}")
        return "\n".join(lines)


def _host_refusals(alias: str, facts: dict[str, str]) -> list[str]:
    hold = facts["hold"] or '{"hold": {"status": "unreadable", "maintenance": null}}'
    status, phase = split_hold_text(hold)
    reasons: list[str] = []
    if facts["dirty"] != "0":
        reasons.append(f"{alias}: $HOME/.meiva/source has uncommitted changes")
    if facts["hook"] == "yes":
        reasons.append(f"{alias}: a post-checkout hook (the guard recurses); move it aside")
    if facts["active"] == "yes":
        reasons.append(
            f"{alias}: $HOME/.meiva/updates/active names an unfinished release operation"
        )
    stopped = (status, phase) == ("paused", "stopped")
    if status not in _FREE and not stopped:
        reasons.append(f"{alias}: maintenance hold {status}/{phase}")
    return reasons


# meiva:note
# OLD (the one HEAD every host shares before the switch) exists only to diff `.python-version`
# against NEW for the --allow-python-change refusal. It decides nothing about what stops or starts.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
def _preflight(s: Session, args: argparse.Namespace, new: str) -> list[str]:
    reasons: list[str] = []
    heads: dict[str, str] = {}
    for alias in [args.gateway, *args.runner]:
        probe = s.run(alias, PROBE, effect=False).splitlines()
        facts = {key: value for key, _, value in (line.partition("=") for line in probe)}
        heads[alias] = facts["head"]
        reasons += _host_refusals(alias, facts)
    old = {head for head in heads.values() if head != new}
    if len(old) > 1:
        return [*reasons, f"hosts disagree on HEAD: {heads}"]
    resolved = next(iter(old)) if old else None
    if resolved and _git("cat-file", "-e", f"{resolved}^{{commit}}").returncode:
        return [*reasons, f"OLD {resolved} is not in {REPO}: git fetch origin"]
    if resolved:
        stat = _git("diff", "--stat", resolved, new, "--", *_WATCHED).stdout.rstrip()
        s.say(stat or "no change under " + " ".join(_WATCHED))
        python = _git("diff", "--quiet", resolved, new, "--", ".python-version")
        if python.returncode and args.python_change != "--allow-python-change":
            reasons.append(".python-version changes (venvs rebuild): pass --allow-python-change")
    return reasons


# meiva:link cli/docs/fleet-update-window.meiva.okf.md
def _stored_schedules(s: Session, args: argparse.Namespace, new: str) -> None:
    out = s.run(
        args.gateway,
        PRE_VERIFY.format(
            home=HOME,
            new=new,
            read_rows=shlex.quote(read_rows_py()),
            check_rows=shlex.quote(check_rows_py()),
        ),
        effect=False,
    )
    lines = out.splitlines()
    if any(line.startswith("SKIPPED") for line in lines):
        s.say("stored schedule scripts: not checked (see above)")
        return
    code = next((line.partition("=")[2] for line in lines if line.startswith("VERIFY_RC=")), "?")
    if code == "0":
        return
    reds = [line for line in lines if line.startswith(("RED ", "TOOL-ERROR"))]
    what = "red" if code == "1" else f"unevaluable (check exited {code})"
    detail = "\n  ".join(reds) or "no RED line: see the output above"
    if args.red_schedules == "--allow-red-schedules":
        s.say(f"WARNING: stored schedule scripts {what} under {new[:12]}, continuing:\n  {detail}")
        return
    raise FailedError(
        f"refused: stored schedule scripts are {what} under {new[:12]} and would crash-loop after "
        f"`up`:\n  {detail}\nFix them (`meiva schedules update <id> --script-file ...` on the gateway) "
        "and rerun `down`, or pass --allow-red-schedules to proceed anyway."
    )


def silence_program() -> str:
    return Path(__file__).with_name("fleet_alert_silence.py").read_text()


def leases_program() -> str:
    return (Path(__file__).parent / "commands" / "management" / "fleet_leases.py").read_text()


# meiva:link cli/docs/fleet-update-window.meiva.okf.md
def _silence(s: Session, args: argparse.Namespace, verb: str, comment: str = "") -> None:
    tail = (
        f" --hours {args.silence_hours:g} --comment {shlex.quote(comment)}"
        if verb == "open"
        else ""
    )
    out = s.run(args.gateway, f"{_PYTHON} - {verb}{tail}", stdin=silence_program())
    for line in out.splitlines():
        if line.startswith(("SILENCE failed", "SILENCE skipped")):
            s.say(f"WARNING: alert silence not {verb}: {line.removeprefix('SILENCE ')}")


# meiva:note
# Name the takeovers the stops would end, before anything stops -- under --dry-run too.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
def _announce_leases(s: Session, args: argparse.Namespace) -> None:
    try:
        out = s.run(args.gateway, f"{_PYTHON} -", effect=False, stdin=leases_program())
    except FailedError as exc:
        _lease_read_warning(s, str(exc))
        return
    try:
        parsed = json.loads([line for line in out.splitlines() if line.startswith("{")][-1])
        leases = list(parsed["leases"])
    except (IndexError, KeyError, TypeError, ValueError):
        failed = next(
            (line for line in out.splitlines() if line.startswith("LEASES failed")), "no output"
        )
        _lease_read_warning(s, failed)
        return
    if not leases:
        s.say("leases: no active impersonation lease")
        return
    s.say(f"leases: {len(leases)} active impersonation lease(s):")
    for lease in leases:
        s.say(
            f"leases:   agent {lease['agent_id']} session {lease['session_id']} "
            f"executor {lease['executor']!r} expires {lease['expires_at']}"
        )
    s.say(
        "leases: the stop ends every one of them -- a restart never adopts a relay, so the "
        "takeover is aborted mid-task and the native agent resumes: release or renew them first"
    )
    _lease_window(s, args)


# meiva:note
# A lease list that could not be read -- an unreadable table, or a shipped program that exited
# non-zero -- is one warning, never a refusal: refusing here would be the very thing the
# announcement exists to prevent, a window that cannot run.
def _lease_read_warning(s: Session, detail: str) -> None:
    s.say(
        f"WARNING: active impersonation leases were not read ({detail}); a takeover this window "
        "ends goes unannounced"
    )


# meiva:note
# Hold the stops open for as long as the operator asked; 0 reports the leases and returns at once.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
def _lease_window(s: Session, args: argparse.Namespace) -> None:
    if not args.lease_confirm_seconds:
        return
    if s.dry_run:
        s.say(f"leases: dry run: not waiting {args.lease_confirm_seconds}s")
        return
    deadline = time.monotonic() + args.lease_confirm_seconds
    while (left := deadline - time.monotonic()) > 0:
        s.say(f"leases: {left:.0f}s left: release or renew them, or let the stop end them")
        time.sleep(min(LEASE_TICK_S, left))


# meiva:link cli/docs/fleet-update.meiva.okf.md
def down(s: Session, args: argparse.Namespace) -> None:
    new = _git("rev-parse", "--verify", f"{args.new}^{{commit}}").stdout.strip()
    if not new:
        raise FailedError(f"refused: {args.new} is not a commit in {REPO}; git fetch origin")
    reasons = _preflight(s, args, new)
    if reasons:
        raise FailedError("refused: " + "; ".join(reasons))
    _announce_leases(s, args)
    everyone = [args.gateway, *args.runner]
    for alias in everyone:
        s.run(alias, f'{HOME}; git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}')
    _stored_schedules(s, args, new)
    stop_flags = _stop_flags(args)
    gateway_stop_flags = f"{stop_flags} {args.infra}"
    _silence(
        s, args, "open", f"fleet update to {new[:12]}: planned outage; `up` expires this silence"
    )
    for alias in [*args.runner, args.gateway]:
        try:
            flags = gateway_stop_flags if alias == args.gateway else stop_flags
            s.run(alias, f"{HOME}; {MEIVA} stop --yes {flags}")
            if not s.dry_run:
                status, phase = split_hold_text(s.run(alias, _STATUS, effect=False))
                if (status, phase) != ("paused", "stopped"):
                    raise FailedError(f"{alias}: stop left {status}/{phase}")
        except FailedError as exc:
            raise FailedError(f"{exc}{_STOP_HINT}") from exc
    for alias in everyone:
        s.run(
            alias,
            f"{HOME}; git checkout -q --detach {new} && {_VENV_DIRS} && "
            f"env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode && {CLEAN}",
        )
    next_step = "continuing with `up`" if args.half == "run" else "run `up` when ready"
    s.say(f"down complete: every host is stopped on {new[:12]}; {next_step}")


def gui_oneshot(alias: str, timeout_s: int, start_flags: str) -> str:
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    label = f"com.meiva.fleet-update.{re.sub(r'[^A-Za-z0-9.-]', '-', alias)}.{stamp}"
    log = f"/tmp/{label}/oneshot.log"
    start = f"{HOME}; {MEIVA} start {start_flags}"
    job = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", "-lc", f'({start}); echo "ONESHOT_RC=$?"'],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return f"""D=/tmp/{label}; mkdir -m 700 "$D" && : > "{log}" || exit 70
cat > "$D/job.plist" <<'MEIVA_FLEET_PLIST'
{plistlib.dumps(job).decode().rstrip()}
MEIVA_FLEET_PLIST
launchctl bootstrap "gui/$(id -u)" "$D/job.plist" || {{ echo "not logged in to the GUI?"; exit 71; }}
n=0; until grep -q '^ONESHOT_RC=' "{log}"; do
  [ "$n" -ge {timeout_s // 5} ] && {{ echo "still running: gui/$(id -u)/{label}, {log}"; exit 72; }}
  sleep 5; n=$((n+1)); done
cat "{log}"; rc=$(sed -n 's/^ONESHOT_RC=//p' "{log}" | tail -n 1)
launchctl bootout "gui/$(id -u)/{label}" || echo "bootout of {label} failed"
rm -rf "$D"; exit "$rc\""""


def machine_names(s: Session, aliases: list[str]) -> dict[str, str]:
    ask = f'{_PYTHON} -c "from base.config import ConfigBoot; from base.cluster.machine.boot import machine_from_boot; print(machine_from_boot(ConfigBoot()).name())"'
    names: dict[str, str] = {}
    for alias in aliases:
        reported = s.run(alias, ask, effect=False).split()
        if not reported:
            raise FailedError(f"{alias}: the host printed no machine name")
        names[alias] = reported[-1]
    if len(set(names.values())) != len(names):
        raise FailedError(f"two listed hosts report one machine name: {names}")
    return names


# meiva:link cli/docs/fleet-update.meiva.okf.md
def _roster(s: Session, args: argparse.Namespace, program: str) -> tuple[list[dict[str, Any]], str]:
    names = machine_names(s, [args.gateway, *args.runner])
    wanted = set(names.values())
    deadline = time.monotonic() + args.roster_timeout
    while True:
        out = s.run(args.gateway, f"{program} roster", effect=False, stdin=gateway_program())
        rows = json.loads(out.splitlines()[-1])
        if missing := sorted(wanted - {row["name"] for row in rows}):
            raise FailedError(f"no roster row for {missing} (host aliases to names: {names})")
        listed = [row for row in rows if row["name"] in wanted]
        offline = [row["name"] for row in listed if not row["online"] or row["identity_mismatch"]]
        commits = {(row["head_sha"], row["running_sha"]) for row in listed}
        commit = listed[0]["head_sha"]
        if not offline and commit is not None and commits == {(commit, commit)}:
            _report_unlisted(s, rows, wanted)
            return listed, commit
        if time.monotonic() >= deadline:
            _report_unlisted(s, rows, wanted)
            raise FailedError(
                f"roster after {args.roster_timeout}s: offline {offline}; commits {commits}"
            )
        time.sleep(POLL_S)


def _report_unlisted(s: Session, rows: list[dict[str, Any]], wanted: set[str]) -> None:
    for row in rows:
        if row["name"] not in wanted:
            s.say(
                f"roster: {row['name']} is not listed, not checked: online={row['online']} "
                f"head={row['head_sha']} running={row['running_sha']}"
            )


def _refresh(s: Session, args: argparse.Namespace) -> None:
    for alias in [args.gateway, *args.runner]:
        out = s.run(alias, f"{HOME}; {MEIVA} packages refresh")
        if out:
            s.say(f"packages refresh [{alias}] {out.splitlines()[-1].strip()}")


def _drift_checks(s: Session, args: argparse.Namespace) -> None:
    checks = [(args.gateway, "schedules verify --no-notify")]
    checks += [(alias, "plugins verify") for alias in [args.gateway, *args.runner]]
    failed: list[str] = []
    for alias, verb in checks:
        try:
            s.run(alias, f"{HOME}; {MEIVA} {verb}", effect=True)
        except FailedError as exc:
            failed.append(f"`meiva {verb}` {exc}")
    if failed:
        raise FailedError("drift check failed: " + "; ".join(failed))


# meiva:note
# Exit code 2 already means "refused before doing anything"; a run where some
# hosts came up and others did not is its own outcome, so automation can tell
# a partial up from a rejected one.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
PARTIAL_FAILURE_EXIT = 3

_INCOMPLETE = 'cat "$HOME/.meiva/state/converge-incomplete.json" 2>/dev/null'


@dataclass(frozen=True)
class HostFailure:
    alias: str
    step: str
    reason: str

    def line(self) -> str:
        return f"{self.alias} failed at {self.step!r}: {self.reason}"


def _host_failure(s: Session, alias: str, exc: FailedError) -> HostFailure:
    try:
        recorded = json.loads(s.run(alias, _INCOMPLETE, effect=False))
        return HostFailure(alias, str(recorded["step"]), str(recorded["error"]))
    except (FailedError, ValueError, KeyError, TypeError):
        return HostFailure(alias, "start", str(exc))


# meiva:note
# One host's converge failure must not stop the rest: each start is attempted,
# the failures are collected with their step and reason, and the caller reports
# them and exits 3. A half-up cluster is reported, never hidden behind a single
# abort that leaves the remaining hosts untouched.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
def up(s: Session, args: argparse.Namespace) -> list[HostFailure]:
    failures: list[HostFailure] = []
    start_flags = _start_flags(args)
    for alias in [args.gateway, *args.runner]:
        macos = s.os[alias] == "Darwin"
        try:
            s.run(
                alias,
                gui_oneshot(alias, args.start_timeout, start_flags)
                if macos
                else f"{HOME}; {MEIVA} start {start_flags}",
            )
            if (
                not s.dry_run
                and (hold := split_hold_text(s.run(alias, _STATUS, effect=False)))[0] not in _FREE
            ):
                raise FailedError(f"start left maintenance {hold}")
        except FailedError as exc:
            failure = _host_failure(s, alias, exc)
            failures.append(failure)
            s.say(f"PARTIAL: {failure.line()}")
    if failures:
        _silence(s, args, "close")
        return failures
    program = f"{_PYTHON} -"
    if s.dry_run:
        s.run(args.gateway, f"{program} smoke MACHINE {args.smoke_timeout}  # each agent-runner")
        _refresh(s, args)
        _drift_checks(s, args)
        _silence(s, args, "close")
        return []
    rows, commit = _roster(s, args, program)
    for name in [row["name"] for row in rows if row["serve_agent_runner"]]:
        smoke = f"{program} smoke {shlex.quote(name)} {args.smoke_timeout}"
        s.run(args.gateway, smoke, stdin=gateway_program())
    _refresh(s, args)
    _drift_checks(s, args)
    _silence(s, args, "close")
    s.say(f"up complete: every listed machine runs {commit[:12]}")
    return []


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
# Every lifecycle choice is an explicit flag with no default and no inference. A pair is two
# store-const flags on one dest (argparse's required mutually exclusive group would report only the
# first missing one), so `_missing` can name every absent choice at once before any host is touched.
_PAIRS: dict[str, tuple[str, str]] = {
    "shell_sessions": ("--keep-shell-sessions", "--close-shell-sessions"),
    "browser": ("--keep-browser", "--stop-browser"),
    "extras": ("--keep-extras", "--stop-extras"),
    "infra": ("--keep-infra", "--stop-infra"),
    "python_change": ("--allow-python-change", "--refuse-python-change"),
    "red_schedules": ("--allow-red-schedules", "--refuse-red-schedules"),
    "execution": ("--dry-run", "--execute"),
}
_STOP_PAIRS = ("shell_sessions", "browser", "extras", "infra")
_WINDOW_PAIRS = ("python_change", "red_schedules")


def _add_pair(p: argparse.ArgumentParser, dest: str) -> None:
    for flag in _PAIRS[dest]:
        p.add_argument(flag, dest=dest, action="append_const", const=flag, default=None)


def _missing(args: argparse.Namespace, dests: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    for dest in dests:
        given = getattr(args, dest)
        if given is None:
            problems.append("one of " + " | ".join(_PAIRS[dest]))
        elif len(given) > 1:
            problems.append(f"only one of {' | '.join(_PAIRS[dest])} (got {' and '.join(given)})")
        else:
            setattr(args, dest, given[0])
    return problems


def _missing_choices(args: argparse.Namespace) -> list[str]:
    half = args.half
    problems = _missing(args, ("execution",))
    if args.runner and args.no_runners:
        problems.append("only one of --runner ALIAS | --no-runners")
    elif not args.runner and not args.no_runners:
        problems.append("one of --runner ALIAS (repeatable) | --no-runners")
    if half in ("run", "down"):
        problems += _missing(args, (*_STOP_PAIRS, *_WINDOW_PAIRS))
        if bool(args.keep_service) == args.keep_no_services:
            problems.append("exactly one of --keep-service NAME (repeatable) | --keep-no-services")
        for flag, dest in (
            ("--mode", "mode"),
            ("--stop-timeout", "stop_timeout"),
            ("--silence-hours", "silence_hours"),
            ("--lease-confirm-seconds", "lease_confirm_seconds"),
        ):
            if getattr(args, dest) is None:
                problems.append(flag)
    if half in ("run", "up"):
        chosen = [bool(args.all_services), bool(args.only_service), bool(args.disable_service)]
        if chosen.count(True) != 1:
            problems.append(
                "exactly one of --all-services | --only-service NAME (repeatable) | "
                "--disable-service NAME (repeatable)"
            )
        for flag, dest in (
            ("--start-timeout", "start_timeout"),
            ("--smoke-timeout", "smoke_timeout"),
            ("--roster-timeout", "roster_timeout"),
        ):
            if getattr(args, dest) is None:
                problems.append(flag)
    return problems


def _stop_flags(args: argparse.Namespace) -> str:
    keeps = (
        ["--keep-no-services"]
        if args.keep_no_services
        else [f"--keep-service {shlex.quote(name)}" for name in args.keep_service]
    )
    chosen = [args.shell_sessions, args.browser, args.extras, *keeps]
    return " ".join([*chosen, f"--mode {args.mode}", f"--timeout {args.stop_timeout}"])


def _start_flags(args: argparse.Namespace) -> str:
    if args.all_services:
        return "--all-services"
    if args.only_service:
        return " ".join(f"--only-service {shlex.quote(name)}" for name in args.only_service)
    return " ".join(f"--disable-service {shlex.quote(name)}" for name in args.disable_service)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.fleet_update",
        description="Update a networked source-mode cluster over SSH. Every choice is an explicit "
        "flag: nothing defaults, nothing is inferred, and the stop/start choices are forwarded "
        "unchanged to each host's `meiva stop` / `meiva start`.",
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
        _add_pair(p, "execution")
        if half in ("run", "down"):
            _add_down_arguments(p)
        if half in ("run", "up"):
            _add_up_arguments(p)
    return parser


def _add_down_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--new", required=True, help="commit to switch every host to")
    for dest in (*_STOP_PAIRS, *_WINDOW_PAIRS):
        _add_pair(p, dest)
    p.add_argument("--keep-service", action="append", default=None, metavar="NAME")
    p.add_argument("--keep-no-services", action="store_true")
    p.add_argument("--mode", choices=("smooth", "force"), default=None)
    p.add_argument(
        "--stop-timeout", type=int, default=None, help="forwarded as `meiva stop --timeout`"
    )
    p.add_argument(
        "--lease-confirm-seconds",
        type=_seconds,
        default=None,
        help="seconds to hold the stops open after naming the active impersonation "
        "leases (0: report only, never wait)",
    )
    p.add_argument(
        "--silence-hours",
        type=float,
        default=None,
        help="expiry of the alert silence opened for the window (`up` expires it sooner)",
    )


def _add_up_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--all-services", action="store_true")
    p.add_argument("--only-service", action="append", default=None, metavar="NAME")
    p.add_argument("--disable-service", action="append", default=None, metavar="NAME")
    p.add_argument("--start-timeout", type=int, default=None)
    p.add_argument("--smoke-timeout", type=int, default=None)
    p.add_argument("--roster-timeout", type=int, default=None)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if problems := _missing_choices(args):
        parser.exit(
            2,
            "refused: every choice is explicit; missing or conflicting:\n  "
            + "\n  ".join(problems)
            + "\n",
        )
    runners: list[str] = args.runner or []
    args.runner = runners
    args.dry_run = args.execution == "--dry-run"
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if "MEIVA_AGENT_ID" in os.environ:
        print(
            "refused: run from a plain login shell or an OS-owned job, not an Meiva "
            "agent's shell (a stop closes it); submit the job and return so the agent can drain"
        )
        return 2
    s = Session(args)
    phase = "down" if args.half == "run" else args.half
    try:
        hosts = [args.gateway, *args.runner]
        s.os = {host: s.run(host, "uname -s", effect=False).split()[-1] for host in hosts}
        for phase in ("down", "up") if args.half == "run" else (args.half,):
            s.say(f"phase: {phase}")
            if phase == "down":
                down(s, args)
                continue
            failures = up(s, args)
            if failures:
                for failure in failures:
                    s.say(f"PARTIAL: {failure.line()}")
                s.say(
                    f"up incomplete: {len(failures)} host(s) failed; the rest are serving. "
                    f"Fix each and rerun `up` (idempotent)."
                )
                return PARTIAL_FAILURE_EXIT
    except FailedError as exc:
        s.say(
            f"FAILED: {exc}\nFailed phase: {phase}. Nothing rolled back. "
            f"Fix it, then rerun `{phase}` (idempotent)."
        )
        return 2 if str(exc).startswith("refused") else 1
    finally:
        s.log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
