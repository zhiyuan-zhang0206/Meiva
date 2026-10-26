from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


def test_repair_cli_can_fix_invalid_file_without_authority(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "MEIVA_DB_URL=postgresql://meiva@127.0.0.1:5433/meiva\n"
        "MEIVA_REDIS_URL=redis://127.0.0.1:6379/0\n"
        "MEIVA_EXEC_TIMEOUT_SECONDS=1800\nMEIVA_EXEC_NODE_TIMEOUT_SECONDS=1200\n"
        "MEIVA_OPS_CONCURRENCY=7\nMEIVA_MACHINE_SERVE_GATEWAY=true\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "config",
            "set",
            "MEIVA_EXEC_NODE_TIMEOUT_SECONDS=2000",
            "--local",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={"MEIVA_CONFIG_FETCH": "skip", "MEIVA_HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dotenv_values(path)["MEIVA_EXEC_NODE_TIMEOUT_SECONDS"] == "2000.0"
    assert dotenv_values(path)["MEIVA_OPS_CONCURRENCY"] == "7"
