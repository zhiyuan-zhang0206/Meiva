from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cli import fleet_update
from cli.fleet_update.plan.host_commands import CLEAN
from cli.fleet_update.programs import ALERT_SILENCE_SCRIPT, VERIFY_SCRIPTS, program_text
from cli.fleet_update.session import Pacing
from cli.fleet_update.timers import ProcessTimers


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


_IDLE: dict[str, Any] = {
    "dirty": 0,
    "hook": "no",
    "active": "no",
    "stop_message": "yes",
    "up": True,
}

_FAKE_SSH = Path(__file__).with_name("fake_ssh.py")


# meiva:note
# The `ssh` a window finds on its PATH: a shell wrapper that runs `script` with this interpreter.
# The wrapper is a real executable in a directory the test owns, so the window launches it exactly
# as it launches the operator's `ssh`.
def ssh_on_path(bin_dir: Path, script: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "ssh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -S "{script}" "$@"\n')
    wrapper.chmod(0o755)
    return bin_dir


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
        self.smoked: list[str] = []
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
        return {**base, **self.environ}

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
            "smoked": self.smoked,
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
        self.smoked = data["smoked"]
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
            )
        finally:
            self._adopt(json.loads(self.state_path.read_text()))
        assert not self.violations, self.violations
        return code
