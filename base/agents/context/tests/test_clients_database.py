from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_custom_database_dials_its_own_resource_with_no_process_default(
    db_url: str, tmp_path: Path
) -> None:
    explicit_url = tmp_path / "database-url"
    explicit_url.write_text(db_url)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "base.agents.context.tests._client_database_child",
            str(explicit_url),
        ],
        env={
            "PATH": os.defpath,
            "MEIVA_HOME": str(tmp_path / "home"),
            "MEIVA_DB_URL": "",
            "MEIVA_CONFIG_FETCH": "skip",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
