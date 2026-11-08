from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg

from base.paths import meiva_home

_REPO_ROOT = Path(__file__).resolve().parents[4]


# meiva:note
# The operator command as a real process against the test cluster: its home is a copy of the test
# cluster's configuration, so the command boots, captures its commit and dials the throwaway database
# exactly as it does for an operator.
def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "meiva-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(
        (meiva_home() / ".env").read_text(encoding="utf-8")
        + "\nMEIVA_MACHINE_NAME=rollup-cli-test\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MEIVA_HOME"] = str(home)
    env.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "services.upkeep.events_maintenance.rollup", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_an_inverted_range_is_refused_with_the_usage_message(tmp_path: Path) -> None:
    done = _run_cli(tmp_path, "--from", "20261002", "--to", "20261001")

    assert done.returncode == 2
    assert "--from is after --to" in done.stderr


def test_the_command_rolls_the_range_and_refolds_when_it_reaches_the_watermark(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    db_conn.autocommit = True
    agent = db_conn.execute("INSERT INTO agents (label) VALUES ('cli') RETURNING id").fetchone()
    assert agent is not None
    old: date = datetime.now(UTC).date() - timedelta(days=40)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, tokens_in, "
        "tokens_out) VALUES (%s, %s, 'm', 1, 100, 10)",
        (agent[0], old),
    )
    db_conn.execute("DELETE FROM agent_model_tokens_total_through")
    day = old.strftime("%Y%m%d")

    done = _run_cli(tmp_path, "--from", day, "--to", day)

    assert done.returncode == 0, done.stderr
    assert f"rolled {old}..{old}:" in done.stdout
