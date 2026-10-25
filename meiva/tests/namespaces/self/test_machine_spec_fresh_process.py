# meiva:note
# A machine read resolves no clients and loads no plugins. The in-process spec tests share a
# process whose SDK state earlier tests may already have changed, so this one reads the machine
# in a fresh interpreter and checks that nothing was installed or attempted.
# meiva:link meiva/self/docs/self.meiva.okf.md

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE = Path(__file__).with_name("machine_probe.py")


# meiva:note
# The probe runs a fixed argv from this file; `sys.executable` is the test's own
# interpreter, so the subprocess call carries no shell and no caller input.
def test_a_machine_read_in_a_fresh_process_loads_no_plugins_and_no_database_stack(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_bytes(b"\xff")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MEIVA_AGENT_ID", "MEIVA_CONFIG_BOOT"}
    }
    env["MEIVA_HOME"] = str(tmp_path)
    env["MEIVA_CONFIG_FETCH"] = "skip"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", "-X", "utf8", str(_PROBE), str(_REPO_ROOT)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(line for line in proc.stdout.splitlines() if line.startswith("REPORT "))
    report = json.loads(line.removeprefix("REPORT "))

    assert report == {
        "spec": ["explicit", "owned"],
        "config_prepared": False,
        "load_attempted": False,
        "installed": False,
        "db_stack_loaded": False,
    }
