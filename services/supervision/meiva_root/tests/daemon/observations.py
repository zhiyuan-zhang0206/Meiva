# meiva:note
# Native escalation is progress, not replacement completion. These observations use
# the real control socket and HTTP siblings without changing the supervisor's schedule.
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import cast

import httpx

from base.native_process.root_control.client import RootClient


def serving_port(run_dir: Path, unit_id: str) -> int | None:
    log = run_dir / "logs" / unit_id / "output.log"
    match = re.search(r"Serving HTTP on .* port (\d+)", log.read_text()) if log.exists() else None
    return int(match[1]) if match else None


def assert_serving_siblings(run_dir: Path) -> None:
    for unit_id in ("gateway", "frontend"):
        port = serving_port(run_dir, unit_id)
        assert port is not None
        response = httpx.get(f"http://127.0.0.1:{port}/reply", timeout=1, trust_env=False)
        assert response.status_code == 200 and response.text == "still serving"


def _units(result: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", result["units"])


def status_births(result: dict[str, object]) -> dict[str, tuple[object, ...]]:
    root = cast("dict[str, object]", result["root"])
    return {
        name: (entry["pid"], entry["create_time"], entry["starttime"])
        for name, entry in [("root", root), *((str(unit["id"]), unit) for unit in _units(result))]
    }


def wait_for_replacement(
    client: RootClient,
    proc: subprocess.Popen[bytes],
    log: Path,
    original: tuple[object, ...],
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    last: tuple[object, ...] | None = None
    while time.monotonic() < deadline:
        assert proc.poll() is None, log.read_text()
        current = cast("dict[str, object]", client.status().get("result"))
        unit = next(unit for unit in _units(current) if unit["id"] == "agent-host")
        birth = status_births(current)["agent-host"]
        count = unit["restart_count"]
        last = (birth, unit["state"], unit["restart_failed"], count)
        if (
            birth[0] is not None
            and birth[1] is not None
            and birth != original
            and unit["state"] == "running"
            and unit["restart_failed"] is None
            and isinstance(count, int)
            and count > 0
        ):
            return current
        time.sleep(0.02)
    raise AssertionError(f"agent-host did not publish a new running generation; last={last}")
