from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cli import fleet_update
from cli.fleet_update.plan.host_commands import CLEAN
from cli.fleet_update.plan.release_target import (
    Channel,
    ChannelPointer,
    ReleaseCertificate,
    format_tag_message,
)
from cli.fleet_update.programs import ALERT_SILENCE_SCRIPT, VERIFY_SCRIPTS, program_text
from cli.fleet_update.session import Pacing
from cli.fleet_update.timers import ProcessTimers


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


def certify_commit(
    repo: Path,
    sha: str,
    *,
    version: str = "v2026.10.24.120000",
    channels: tuple[Channel, ...] = ("stable", "latest"),
    hotfix_of: str | None = None,
    source_ref: str = "refs/heads/main",
) -> ReleaseCertificate:
    cert = ReleaseCertificate(
        version=version,
        sha=sha,
        source_ref=source_ref,
        gate_run_id="9001",
        gate_run_url="https://github.com/example/Meiva/actions/runs/9001",
        certified_at="2026-10-24T12:00:00Z",
        hotfix_of=hotfix_of,
    )
    git(repo, "tag", "-f", "-a", version, "-m", format_tag_message(cert), sha)
    for channel in channels:
        pointer = ChannelPointer(
            channel=channel,
            version=version,
            sha=sha,
            updated_at="2026-10-24T12:00:00Z",
        )
        git(
            repo,
            "tag",
            "-f",
            "-a",
            f"channel/{channel}",
            "-m",
            format_tag_message(pointer),
            sha,
        )
    return cert


_IDLE: dict[str, Any] = {
    "dirty": 0,
    "hook": "no",
    "active": "no",
    "stop_message": "yes",
    "up": True,
}

_FAKE_SSH = Path(__file__).with_name("fake_ssh.py")


# meiva:note
# The OS passwd home of the user running pytest. `host_commands.HOME` derives `$H` from `$HOME`,
# ignoring `MEIVA_HOME`, so every host-shell helper refuses to run if `HOME` still resolves here.
# meiva:link cli/tests/fleet_update/docs/fleet-update-harness.meiva.okf.md
def operator_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def assert_isolated_home(home: str | os.PathLike[str] | None) -> None:
    raw = "" if home is None else os.fspath(home).strip()
    if not raw:
        raise AssertionError("refusing to run fleet_update host shell with an empty HOME")
    resolved = Path(raw).resolve()
    if resolved == operator_home():
        raise AssertionError(
            f"refusing to run fleet_update host shell against operator HOME: {resolved}"
        )


def run_host_shell(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_env = dict(os.environ if env is None else env)
    assert_isolated_home(effective_env.get("HOME"))
    return subprocess.run(
        list(argv),
        env=effective_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


# meiva:note
# Write a stub executable in a test-owned directory without ever writing through a symlink (such
# as `.venv/bin/python` pointing into `~/.local/share/uv/python`).
def write_executable(path: Path, body: str) -> Path:
    if path.is_symlink():
        raise AssertionError(f"refusing to write stub executable through symlink: {path}")
    path.unlink(missing_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


# meiva:note
# The `ssh` a window finds on its PATH: a shell wrapper that runs `script` with this interpreter.
# The wrapper is a real executable in a directory the test owns, so the window launches it exactly
# as it launches the operator's `ssh`.
def ssh_on_path(bin_dir: Path, script: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    write_executable(
        bin_dir / "ssh",
        f'#!/bin/sh\nexec "{sys.executable}" -I -S "{script}" "$@"\n',
    )
    return bin_dir


# meiva:note
# The cluster a fleet-update test drives: it writes the state the fake `ssh` reads before each run
# (`_state`) and adopts back what the run left (`_adopt`). Its bookkeeping stays untyped for the
# reason `docs/conventions/testing.md` states — this is the writer half of a program-level double —
# and the field-level schema, every key with its type and meaning, is declared in the reader,
# `cli/tests/fleet_update/fake_ssh.py`; a rename changes both.
#
# What this class owns is the attribute -> JSON mapping. The groups: the fleet (`hosts`, one
# `_IDLE` record per alias plus `os` and `head`, and the roster's `names`, `silent`, `hidden`,
# `laptop`); what a test stages (`fail`, `stop_fails`, `stop_fails_leaves_up`, `recover_fails`,
# `output`, `incomplete`, `roster_reply`, `roster_lag`, `lease_reply`, `lease_rc`,
# `silence_reply`, `stored_schedules`, `barriers`); what the run reports (`effects`, `commands`,
# `run_started_at`, `roster_reads`, `lease_reads`, `silence_open`, `silence_hours`,
# `silence_comment`, `violations`); and the healthy baseline every host reports (`host_up`,
# `journal`, `journal_after_stop`, `helper_hash`, `helper_cert_root`).
# meiva:link cli/tests/fleet_update/docs/fleet-update-harness.meiva.okf.md
class Cluster:
    def __init__(self, old: str, new: str, *, repo: Path, workdir: Path) -> None:
        self.old = old
        self.new = new
        self.repo = repo
        self.workdir = workdir
        self.bin_dir = ssh_on_path(workdir / "bin", _FAKE_SSH)
        self.state_path = workdir / "cluster-state.json"
        self.environ: dict[str, str] = {}
        self.pacing = Pacing(roster_poll_s=0.01, lease_tick_s=0.25)
        self.invocations = 0
        self.hosts: dict[str, dict[str, Any]] = {
            a: {**_IDLE, "os": o, "head": old}
            for a, o in (("gw", "Linux"), ("mac", "Darwin"), ("lin", "Linux"))
        }
        self.names = {alias: alias for alias in self.hosts}
        self.silent: set[str] = set()
        self.hidden: set[str] = set()
        self.laptop: dict[str, Any] | None = None
        self.effects: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str]] = []
        self.run_started_at = 0
        self.fail: dict[tuple[str, str], int] = {}
        self.output = ""
        self.stored_schedules: tuple[int, list[str]] = (0, [])
        self.roster_lag = 0
        self.roster_reply: str | None = None
        self.incomplete: dict[str, str] = {}
        self.roster_reads = 0
        self.silence_open = False
        self.silence_hours = 0.0
        self.silence_comment = ""
        self.silence_reply: str | None = None
        self.leases: list[dict[str, Any]] = []
        self.lease_reply: str | None = None
        self.lease_rc = 0
        # meiva:note
        # The daemon each host still runs, the last stop journal its lifecycle file holds, and the
        # two stop failures a test can stage: one that takes the daemon with it (the #5438 shape)
        # and one that leaves it running (a failed stop is not automatically an outage).
        # meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
        self.host_up: dict[str, bool] = dict.fromkeys(self.hosts, True)
        self.journal: dict[str, dict[str, Any] | None] = dict.fromkeys(self.hosts)
        self.stop_fails: dict[str, int] = {}
        # meiva:note
        # A host whose `meiva stop` exits 0 while its journal records no completed stop (the
        # fleet judges a stop by both): the journal the host shows after the stop, in place of
        # the one the stop wrote.
        self.journal_after_stop: dict[str, dict[str, Any] | None] = {}
        self.stop_fails_leaves_up: set[str] = set()
        self.recover_fails: set[str] = set()
        # meiva:note
        # Where each read happened: the effects already accepted, so a test can prove the leases
        # are named before anything stops.
        # meiva:link cli/tests/fleet_update/docs/fleet-update-harness.meiva.okf.md
        self.lease_reads: list[int] = []
        # meiva:note
        # The helper every host reports: current for the target commit and not running, so a
        # test stages only the one host it is about (a stale or loaded helper, another
        # certificate) and the rest stay as a healthy fleet would report them.
        self.helper_hash = "a" * 64
        self.helper_cert_root = "f" * 40
        self.barriers: dict[str, list[str]] = {}
        self.violations: list[str] = []

    def process_env(self) -> dict[str, str]:
        base = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.workdir),
            "FLEET_CLUSTER_STATE": str(self.state_path),
        }
        merged = {**base, **self.environ}
        assert_isolated_home(merged.get("HOME"))
        return merged

    def _state(self) -> dict[str, Any]:
        return {
            "old": self.old,
            "new": self.new,
            "clean": CLEAN,
            "programs": {
                "gateway": program_text(VERIFY_SCRIPTS / "fleet_gateway.py"),
                "silence": program_text(ALERT_SILENCE_SCRIPT),
                "leases": program_text(VERIFY_SCRIPTS / "fleet_leases.py"),
            },
            "hosts": self.hosts,
            "names": self.names,
            "silent": sorted(self.silent),
            "hidden": sorted(self.hidden),
            "laptop": self.laptop,
            "effects": self.effects,
            "commands": self.commands,
            "run_started_at": self.run_started_at,
            "fail": {f"{kind}|{alias}": rc for (kind, alias), rc in self.fail.items()},
            "output": self.output,
            "stored_schedules": list(self.stored_schedules),
            "roster_lag": self.roster_lag,
            "roster_reply": self.roster_reply,
            "incomplete": self.incomplete,
            "roster_reads": self.roster_reads,
            "silence_open": self.silence_open,
            "silence_hours": self.silence_hours,
            "silence_comment": self.silence_comment,
            "silence_reply": self.silence_reply,
            "leases": self.leases,
            "lease_reply": self.lease_reply,
            "lease_rc": self.lease_rc,
            "host_up": self.host_up,
            "journal": self.journal,
            "stop_fails": self.stop_fails,
            "journal_after_stop": self.journal_after_stop,
            "stop_fails_leaves_up": sorted(self.stop_fails_leaves_up),
            "recover_fails": sorted(self.recover_fails),
            "lease_reads": self.lease_reads,
            "helper_hash": self.helper_hash,
            "helper_cert_root": self.helper_cert_root,
            "barriers": self.barriers,
            "violations": self.violations,
        }

    def _adopt(self, data: dict[str, Any]) -> None:
        self.hosts = data["hosts"]
        self.effects = [(kind, alias) for kind, alias in data["effects"]]
        self.commands = [(alias, command) for alias, command in data["commands"]]
        self.roster_reads = data["roster_reads"]
        self.silence_open = data["silence_open"]
        self.silence_hours = data["silence_hours"]
        self.silence_comment = data["silence_comment"]
        self.host_up = data["host_up"]
        self.journal = data["journal"]
        self.lease_reads = data["lease_reads"]
        self.roster_lag = data["roster_lag"]
        self.violations = data["violations"]

    # meiva:note
    # Run `fleet_update.main` against this cluster: write the state the fake `ssh` will read,
    # run the window with the fake first on its PATH, read back what the hosts did, and fail the
    # test on any order production forbids. Each invocation is stamped one second later than the
    # last, so two windows in one test never collide on a log name.
    def invoke(self, argv: list[str]) -> int:
        self.run_started_at = len(self.effects)
        self.state_path.write_text(json.dumps(self._state()))
        now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=self.invocations)
        self.invocations += 1
        try:
            code = fleet_update.main(
                argv,
                repo=self.repo,
                env=self.process_env(),
                now=now,
                pacing=self.pacing,
                timers=ProcessTimers(),
                operation_id=f"op-{self.invocations:04d}",
                initiator="user:test@test-host",
                python=sys.executable,
            )
        finally:
            self._adopt(json.loads(self.state_path.read_text()))
        assert not self.violations, self.violations
        return code
