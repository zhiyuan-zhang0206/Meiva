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
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from subprocess import PIPE, STDOUT
from typing import Any, cast

from .fleet_gate import (
    REBUILD_STOP,
    baselines,
    failed_stop,
    host_refusals,
    probe_helper,
    probe_host,
    rebuilding_hosts,
    refusals,
    stop_completed,
)
from .fleet_verify import POLL_S as POLL_S
from .fleet_verify import (
    FailedError,
    HostFailure,
    outcome,
    verify,
)

REPO = REPO_CODE = Path(__file__).resolve().parents[2]
HOME = 'H="$HOME/.meiva"; S="$H/source"; cd "$S" || exit 1'
MEIVA = 'MEIVA_HOME="$H" "$S/.venv/bin/meiva"'
_PYTHON = f'{HOME}; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python"'

# meiva:note
# Five lines a minute: enough for an operator to watch the window move, few enough not to bury the
# leases named above it.
LEASE_TICK_S = 5.0
_STOP_HINT = (
    "\nOn that host retry `meiva stop` with the same arguments this `down` gave it (the logged "
    "command above), confirm `run/lifecycle-op.json` records a completed stop (operation `stop`, "
    "complete, rc 0), then rerun `down`."
)
CLEAN = (
    'd=$(git status --porcelain | head -n 20); [ -z "$d" ] || { echo "$d"; echo "Untracked files '
    "are not caused by this update (a changed .gitignore can reveal them): check them, then "
    'move them away (do not delete) and rerun down."; exit 1; }'
)
_OP_EVIDENCE = """H="$HOME/.meiva"
p=$(cat "$H/run/agent_host.pid" 2>/dev/null)
if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo host=up; else echo host=down; fi
cat "$H/run/lifecycle-op.json" 2>/dev/null || true"""
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


VERIFY_SCRIPTS = Path(__file__).resolve().parents[1] / "commands" / "management"
CHECK_ROWS_SCRIPT = Path(__file__).with_name("schedules_verify_check_rows.py")
ALERT_SILENCE_SCRIPT = Path(__file__).resolve().parents[1] / "fleet_alert_silence.py"


def program_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def _git(*args: str, repo: Path = REPO) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


class Session:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        repo: Path = REPO,
        ssh_fn: Callable[[str, str, str | None, Callable[[str], None]], int] = ssh,
        clock: Any = time,
    ) -> None:
        self.dry_run: bool = args.dry_run
        self.repo: Path = repo
        self.ssh_fn = ssh_fn
        self.clock: Any = clock
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

        if rc := self.ssh_fn(alias, script, stdin, emit):
            raise FailedError(f"{alias}: exit {rc}: {lines[-1] if lines else 'no output'}")
        return "\n".join(lines)


# meiva:note
# OLD is the one commit every host agrees on that is not NEW; hosts that disagree, or an OLD the
# operator's checkout does not carry, refuse the window. OLD only feeds the `.python-version`
# refusal: it decides nothing about what stops or starts.
def _old_target(
    s: Session, args: argparse.Namespace, heads: dict[str, str], new: str
) -> tuple[list[str], str | None]:
    older = {head for head in heads.values() if head != new}
    if len(older) > 1:
        return [f"hosts disagree on HEAD: {heads}"], None
    resolved = next(iter(older)) if older else None
    if resolved and _git("cat-file", "-e", f"{resolved}^{{commit}}", repo=s.repo).returncode:
        return [f"OLD {resolved} is not in {s.repo}: git fetch origin"], None
    reasons: list[str] = []
    if resolved:
        stat = _git("diff", "--stat", resolved, new, "--", *_WATCHED, repo=s.repo).stdout.rstrip()
        s.say(stat or "no change under " + " ".join(_WATCHED))
        python = _git("diff", "--quiet", resolved, new, "--", ".python-version", repo=s.repo)
        if python.returncode and args.python != "--allow-python-change":
            reasons.append(".python-version changes (venvs rebuild): pass --allow-python-change")
    return reasons, resolved


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _preflight(
    s: Session, args: argparse.Namespace, new: str
) -> tuple[list[str], str | None, list[str], set[str]]:
    reasons: list[str] = []
    heads: dict[str, str] = {}
    serving: list[str] = []
    for alias in [args.gateway, *args.runner]:
        facts, was_serving = probe_host(s, alias)
        heads[alias] = facts["head"]
        reasons += host_refusals(alias, facts)
        if was_serving:
            serving.append(alias)
    more, resolved = _old_target(s, args, heads, new)
    if reasons or more:
        return [*reasons, *more], resolved, serving, set[str]()
    # meiva:note
    # The helper gate reads NEW, so every host must hold it first. This fetch is a
    # non-destructive effect the window deliberately accepts before the gate: the gate's own
    # question is about that commit, and the cheap refusals above have already passed.
    if args.dry_run:
        s.say("helper gate: NOT evaluated (dry run: the target commit is not fetched)")
        return [], resolved, serving, set[str]()
    everyone = [args.gateway, *args.runner]
    for alias in everyone:
        s.run(alias, f'{HOME}; git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}')
    probes = {alias: probe_helper(s, alias, new) for alias in everyone}
    rebuild = bool(getattr(args, "rebuild_extras", False))
    table = baselines(s.repo)
    return (
        refusals(probes, table, rebuild=rebuild),
        resolved,
        serving,
        rebuilding_hosts(probes, rebuild=rebuild),
    )


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _stored_schedules(s: Session, args: argparse.Namespace, new: str) -> None:
    out = s.run(
        args.gateway,
        program_text(VERIFY_SCRIPTS / "fleet_pre_verify.sh").format(
            home=HOME,
            new=new,
            read_rows=shlex.quote(program_text(VERIFY_SCRIPTS / "schedules_verify_read_rows.py")),
            check_rows=shlex.quote(program_text(CHECK_ROWS_SCRIPT)),
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
    if args.red == "--allow-red-schedules":
        s.say(f"WARNING: stored schedule scripts {what} under {new[:12]}, continuing:\n  {detail}")
        return
    raise FailedError(
        f"refused: stored schedule scripts are {what} under {new[:12]} and would crash-loop after "
        f"`up`:\n  {detail}\nFix them (`meiva schedules update <id> --script-file ...` on the gateway) "
        "and rerun `down`, or pass --allow-red-schedules to proceed anyway."
    )


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _silence(s: Session, args: argparse.Namespace, verb: str, comment: str = "") -> None:
    hours = getattr(args, "silence_hours", None)
    tail = f" --hours {hours:g} --comment {shlex.quote(comment)}" if verb == "open" else ""
    out = s.run(
        args.gateway,
        f"{_PYTHON} - {verb}{tail}",
        stdin=program_text(ALERT_SILENCE_SCRIPT),
    )
    for line in out.splitlines():
        if line.startswith(("SILENCE failed", "SILENCE skipped")):
            s.say(f"WARNING: alert silence not {verb}: {line.removeprefix('SILENCE ')}")


# meiva:note
# Name the takeovers the stops would end, before anything stops -- under --dry-run too.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _announce_leases(s: Session, args: argparse.Namespace) -> None:
    try:
        out = s.run(
            args.gateway,
            f"{_PYTHON} -",
            effect=False,
            stdin=program_text(VERIFY_SCRIPTS / "fleet_leases.py"),
        )
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
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _lease_window(s: Session, args: argparse.Namespace) -> None:
    if not args.lease_confirm_seconds:
        return
    if s.dry_run:
        s.say(f"leases: dry run: not waiting {args.lease_confirm_seconds}s")
        return
    deadline = s.clock.monotonic() + args.lease_confirm_seconds
    while (left := deadline - s.clock.monotonic()) > 0:
        s.say(f"leases: {left:.0f}s left: release or renew them, or let the stop end them")
        s.clock.sleep(min(LEASE_TICK_S, left))


# meiva:note
# A failed stop must not leave the cluster half stopped: that hosts this run took down go back up,
# on the OLD commit they still carry, so a failed window costs an aborted update instead of an
# outage. Three facts decide one host, and none of them alone is enough: it was serving when the
# window began (a host the operator had already stopped stays stopped), it is down now, and this
# run is what took it down -- either its stop completed here, or the host's own stop journal
# records a failed or unfinished stop. A host that is still running is never touched, and no code
# is rolled back: the switch phase never started.
# The start is `meiva start` with the operator's explicit selection, never `--boot`: a boot attempt spends the boot budget, and on
# macOS the failing attempt that spends it exits 0 (launchd has no attempt limit), so a failed
# recovery would read as success and never reach `needs manual:`.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _recover_afterfailed_stop(
    s: Session,
    args: argparse.Namespace,
    serving: list[str],
    stopped: list[str],
    failed: str | None,
) -> None:
    recovered: list[str] = []
    down: list[str] = []
    for alias in [args.gateway, *args.runner]:
        if alias not in serving:
            continue
        up, journal = _host_up_and_journal(s, alias)
        if up:
            continue
        down.append(alias)
        if alias not in stopped and not (alias == failed and failed_stop(journal)):
            continue
        try:
            flags = _start_flags(args)
            start = (
                gui_oneshot(alias, args.start_timeout, flags)
                if s.os[alias] == "Darwin"
                else f"{HOME}; {MEIVA} start {flags}"
            )
            s.run(alias, start)
            recovered.append(alias)
        except FailedError as exc:
            s.say(f"recovery: {exc}")
    s.say(f"recovered: {', '.join(recovered) or 'none'}")
    s.say(f"needs manual: {', '.join(a for a in down if a not in recovered) or 'none'}")


# meiva:note
# The host's daemon state and its last lifecycle journal, in one read: the journal answers "was
# this host's stop a failure" (a stop that reported a non-zero rc or never completed), which is
# the half a running daemon cannot tell us.
def _host_up_and_journal(s: Session, alias: str) -> tuple[bool, dict[str, Any] | None]:
    lines = s.run(alias, _OP_EVIDENCE, effect=False).splitlines()
    up = any(line.strip() == "host=up" for line in lines)
    raw = next((line for line in lines if line.startswith("{")), None)
    try:
        parsed: Any = json.loads(raw) if raw is not None else None
    except ValueError:
        parsed = None
    return up, cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


# meiva:link cli/docs/fleet-update.meiva.okf.md
def down(s: Session, args: argparse.Namespace) -> None:
    new = _git("rev-parse", "--verify", f"{args.new}^{{commit}}", repo=s.repo).stdout.strip()
    if not new:
        raise FailedError(f"refused: {args.new} is not a commit in {s.repo}; git fetch origin")
    reasons, _old, serving, rebuilding = _preflight(s, args, new)
    if reasons:
        raise FailedError("refused: " + "; ".join(reasons))
    _announce_leases(s, args)
    everyone = [args.gateway, *args.runner]
    _stored_schedules(s, args, new)
    chosen = [args.shell, args.browser, args.extras, *_flags(args, args.kept)]
    stop_flags = " ".join([*chosen, f"--mode {args.mode}", f"--timeout {args.stop_timeout}"])
    _silence(
        s, args, "open", f"fleet update to {new[:12]}: planned outage; `up` expires this silence"
    )
    stopped: list[str] = []
    current = args.gateway
    try:
        for alias in [*args.runner, args.gateway]:
            current = alias
            flags = f"{stop_flags} {args.infra}" if alias == args.gateway else stop_flags
            if alias in rebuilding:
                s.say(f"{alias}: retiring extras so its next start can rebuild the helper")
                flags = f"{REBUILD_STOP} --mode {args.mode} --timeout {args.stop_timeout}"
                if alias == args.gateway:
                    flags = f"{flags} {args.infra}"
            s.run(alias, f"{HOME}; {MEIVA} stop --yes {flags}")
            if not s.dry_run and not stop_completed(_host_up_and_journal(s, alias)[1]):
                raise FailedError(
                    f"{alias}: stop exited 0 but its journal records no completed stop"
                )
            stopped.append(alias)
    except FailedError as exc:
        if not s.dry_run:
            _recover_afterfailed_stop(s, args, serving, stopped, current)
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


# meiva:note
# Exit code 2 already means "refused before doing anything"; a run where some
# hosts came up and others did not is its own outcome, so automation can tell
# a partial up from a rejected one.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
PARTIAL_FAILURE_EXIT = 3

_INCOMPLETE = 'cat "$HOME/.meiva/state/converge-incomplete.json" 2>/dev/null'


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
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def up(s: Session, args: argparse.Namespace) -> list[HostFailure]:
    # meiva:note
    # Start every host, then verify every host: one failure never erases what the window learned
    # about the others, and each listed host ends with its own line.
    failures: list[HostFailure] = []
    hosts = [args.gateway, *args.runner]
    start_flags = _start_flags(args)
    started: set[str] = set()
    for alias in hosts:
        macos = s.os[alias] == "Darwin"
        try:
            s.run(
                alias,
                gui_oneshot(alias, args.start_timeout, start_flags)
                if macos
                else f"{HOME}; {MEIVA} start {start_flags}",
            )
        except FailedError as exc:
            failure = _host_failure(s, alias, exc)
            failures.append(failure)
            s.say(f"PARTIAL: {failure.line()}")
        else:
            started.add(alias)
    verified, skipped, commit = verify(
        s,
        args,
        hosts=hosts,
        started=started,
        program=f"{_PYTHON} -",
        gateway_stdin=program_text(VERIFY_SCRIPTS / "fleet_gateway.py"),
        clock=s.clock,
    )
    failures += verified
    # meiva:note
    # The planned-outage silence ends here even on a partial up: hosts that came back healthy
    # must not spend the rest of the silence with their alerts suppressed.
    _silence(s, args, "close")
    for alias in hosts:
        s.say(f"matrix: {alias} {outcome(alias, failures, skipped)}")
    if not failures:
        s.say(f"up complete: every listed machine runs {commit[:12]}")
    return failures


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
def _service_problems(args: argparse.Namespace) -> list[str]:
    named = {n for flag in _NAMED for n in getattr(args, flag[2:].replace("-", "_"), None) or ()}
    own = sorted(set(getattr(args, "keep_service", None) or ()) & {"pty-sessions", "browser"})
    problems = [f"--keep-service {name}: it has its own keep/stop choice" for name in own]
    if not named:
        return problems
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "MEIVA_HOME": home, "MEIVA_CONFIG_FETCH": "skip"}
        run, here = [sys.executable, "-m", "services.supervision.roster"], REPO_CODE
        done = subprocess.run(run, cwd=here, env=env, capture_output=True, text=True, check=False)
    known = set(done.stdout.split()) if done.returncode == 0 else set[str]()
    unknown = sorted(named - known)
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
    "python": ("--allow-python-change", "--refuse-python-change"),
    "red": ("--allow-red-schedules", "--refuse-red-schedules"),
}
# meiva:note
# Every half names the start selection and deadline: `up` and a failed `down`'s recovery use them.
_HALF_PAIRS: dict[str, list[str]] = {
    "both": ["execution", "selection"],
    "down": ["shell", "browser", "extras", "infra", "kept", "python", "red"],
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
            p.add_argument("--new", required=True, help="commit to switch every host to")
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
                    else:
                        p.add_argument(flag, dest=dest, action="append_const", const=flag)
            for flag, kind in _HALF_VALUES[scope]:
                p.add_argument(flag, type=kind, default=None)
    return parser


def _value(args: argparse.Namespace, flag: str) -> Any:
    return getattr(args, flag[2:].replace("-", "_"))


def _value_problems(args: argparse.Namespace, scope: str) -> list[str]:
    problems: list[str] = []
    for flag, kind in _HALF_VALUES[scope]:
        if (value := _value(args, flag)) is None or (kind in (int, float) and value <= 0):
            problems.append(flag if value is None else f"{flag} {value}: not a number > 0")
    return problems


def _missing_choices(args: argparse.Namespace) -> list[str]:
    problems = [] if bool(args.runner) != args.no_runners else ["--runner ALIAS... | --no-runners"]
    for scope in _SCOPES[args.half]:
        for dest in _HALF_PAIRS[scope]:
            given = [f for f in _PAIRS[dest] if f in _NAMED and _value(args, f)]
            given += getattr(args, dest, None) or []
            if len(given) != 1:
                problems.append("exactly one of " + " | ".join(_PAIRS[dest]))
            else:
                setattr(args, dest, given[0])
        problems += _value_problems(args, scope)
    if args.half != "up" and args.mode not in (None, "smooth", "force"):
        problems.append("--mode smooth|force")
    return problems + _service_problems(args)


def _flags(args: argparse.Namespace, chosen: str) -> list[str]:
    if chosen in _NAMED:
        return [f"{chosen} {shlex.quote(name)}" for name in _value(args, chosen)]
    return [chosen]


def _start_flags(args: argparse.Namespace) -> str:
    return " ".join(_flags(args, args.selection))


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if problems := _missing_choices(args):
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


def main(
    argv: list[str] | None = None,
    *,
    repo: Path = REPO,
    ssh_fn: Callable[[str, str, str | None, Callable[[str], None]], int] = ssh,
    clock: Any = time,
    env: Mapping[str, str] | None = None,
) -> int:
    args = _parse(argv)
    effective_env = os.environ if env is None else env
    if "MEIVA_AGENT_ID" in effective_env:
        print(
            "refused: run from a plain login shell or an OS-owned job, not an Meiva "
            "agent's shell (a stop closes it); submit the job and return so the agent can drain"
        )
        return 2
    s = Session(args, repo=repo, ssh_fn=ssh_fn, clock=clock)
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
