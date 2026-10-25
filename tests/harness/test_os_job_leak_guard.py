"""The session-level leak guard's inventory (`tests/_os_jobs.py`).

`pytest_sessionfinish` diffs `host_meiva_os_jobs()` against the snapshot taken at
provisioning-plugin import and fails the run on anything new or changed. These
tests cover what that diff can see — the guard itself only runs at session end,
where a self-test cannot observe it. It removes nothing: a job's label names the
job, not a home, so a leaked one replaced the host's real job.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests import _os_jobs


def _plant(home: Path, label: str, body: str = "<plist/>") -> Path:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_text(body)
    return p


def _write_fake_crontab(bin_dir: Path, lines: tuple[str, ...] = ()) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    crontab = bin_dir / "crontab"
    payload = "".join(f"printf '%s\\n' '{line}'\n" for line in lines)
    crontab.write_text(f'#!/bin/sh\nif [ "$1" != "-l" ]; then exit 2; fi\n{payload}exit 0\n')
    crontab.chmod(0o700)


def _run_inventory(
    home: Path,
    *,
    kind: str = "host",
    crontab_lines: tuple[str, ...] = (),
) -> frozenset[str]:
    # meiva:note
    # Run the OS job inventory in a child process with an isolated HOME and PATH so tests never patch process globals.
    bin_dir = home / ".fake-bin"
    _write_fake_crontab(bin_dir, crontab_lines)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
    }
    res = subprocess.run(  # noqa: S603
        [sys.executable, __file__, kind],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return frozenset(json.loads(res.stdout))


def test_sees_a_planted_launchagent(tmp_path: Path) -> None:
    _plant(tmp_path, "com.meiva.autostart")
    (job,) = _run_inventory(tmp_path)
    assert job.startswith("launchd:com.meiva.autostart.plist@")


def test_a_rewritten_job_is_a_new_inventory_entry(tmp_path: Path) -> None:
    """A fixed label means a leak overwrites the host's real job rather than
    adding a second one, so the id must change with the plist's content."""
    plist = _plant(tmp_path, "com.meiva.logs-maintenance", "<real-plist/>")
    before = _run_inventory(tmp_path)
    assert _run_inventory(tmp_path) == before
    plist.write_text("<a-test-wrote-this/>")
    after = _run_inventory(tmp_path)
    assert after != before and len(after) == 1


def test_ignores_plists_that_are_not_ours(tmp_path: Path) -> None:
    """`com.meiva.*` only — a developer's unrelated LaunchAgents must never show up
    as a leak."""
    _plant(tmp_path, "com.example.something")
    assert _run_inventory(tmp_path) == frozenset()


def test_no_launchagents_dir_is_not_an_error(tmp_path: Path) -> None:
    assert _run_inventory(tmp_path) == frozenset()


def test_only_meiva_marked_crontab_lines_are_inventoried(tmp_path: Path) -> None:
    mine = "*/5 * * * * /x/meiva cluster health-probe # meiva-health-probe"
    theirs = "0 3 * * * /usr/bin/backup"
    assert _run_inventory(tmp_path, kind="crontab", crontab_lines=(theirs, mine)) == frozenset(
        {f"crontab:{mine}"}
    )


def _child_main() -> None:
    kind = sys.argv[1]
    if kind == "host":
        sys.stdout.write(json.dumps(sorted(_os_jobs.host_meiva_os_jobs())) + "\n")
    elif kind == "crontab":
        sys.stdout.write(json.dumps(sorted(_os_jobs.crontab_jobs())) + "\n")
    else:
        raise ValueError(f"unknown child mode {kind!r}")


if __name__ == "__main__":
    _child_main()
