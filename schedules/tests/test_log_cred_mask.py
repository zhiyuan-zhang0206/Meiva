# meiva:note
# The log-credential sweep run by hand (`--once --no-notify`) against a home of its own: the
# scanner is an operator tool that lives under the home and does not ship in the repository, so a
# home without it ends as one explicit rc=2 line naming the missing path, and the run is logged in
# that home's `logs` directory. No database is involved.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "log-cred-mask-monthly-schedule.py"
_ROOT = _SCRIPT.parent.parent


def test_a_home_without_the_scanner_ends_as_one_explicit_line_and_is_logged_in_that_home(
    tmp_path: Path,
) -> None:
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--once", "--no-notify"],
        cwd=_ROOT,
        env={**os.environ, "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    scanner = tmp_path / "log-cred-mask" / "monthly_sweep.py"
    assert done.returncode == 2, done.stderr
    assert f"ERR no credential scanner at {scanner} on this machine" in done.stdout
    logged = (tmp_path / "logs" / "log-cred-mask-monthly.log").read_text()
    assert "rc=2 sweep=monthly_sweep.py" in logged
