# meiva:note
# A harness child's identity is borrowed on first use, never at import. Three probes,
# each a clean interpreter with the agent-launch environment stripped: a process with
# no handle behaves exactly as before; a process whose registration is dead keeps
# `files` / `shell` and reports the registration's own reason from every read that
# needs an identity; a process with a live registration attaches on its first
# identity read and not during `import meiva` (attaching replays the agent's state
# snapshot, which opens the database — a provider runs a fresh Python per tool call).
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md


from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from meiva.self.harness.registration import (
    HarnessRegistration,
    default_store,
    handle_digest,
    new_handle,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_CLEAN_ENV_STRIP = frozenset(
    {
        "MEIVA_AGENT_ID",
        "MEIVA_PROCESS_PROFILE",
        "MEIVA_EXEC_REQUEST_FILE",
        "MEIVA_EXEC_RESULT_FILE",
        "MEIVA_EXEC_TIMEOUT_S",
        "MEIVA_TURN_ID",
        "MEIVA_SESSION_ID",
        "MEIVA_LOG_DIR",
        "MEIVA_AGENT_LABEL",
        "MEIVA_AGENT_DIR",
        "MEIVA_HARNESS_HANDLE",
    }
)

_PROBE = Path(__file__).with_name("attach_probes.py")


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    path = tmp_path / "marker.txt"
    path.write_text("marker\n", encoding="utf-8")
    return path


# meiva:note
# The probe runs a fixed argv from this file; `sys.executable` is the test's own
# interpreter, so the subprocess call carries no shell and no caller input.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md
def _run_probe(marker: Path, handle: str | None = None) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    env.pop("MEIVA_HARNESS_HANDLE", None)
    if handle is not None:
        env["MEIVA_HARNESS_HANDLE"] = handle
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", "-X", "utf8", str(_PROBE), str(_REPO_ROOT), str(marker)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(line for line in proc.stdout.splitlines() if line.startswith("REPORT "))
    return json.loads(line.removeprefix("REPORT "))


def test_a_process_without_a_handle_is_unchanged(marker: Path) -> None:
    report = _run_probe(marker)

    assert report["has_tasks_at_import"] is False
    assert report["files_read"] == {"ok": "'marker'"}
    assert report["shell_run"] == {"ok": "'hi'"}
    assert report["agent_id"] == {"ok": "None"}
    assert report["context"]["error"] == "ContextOutsideProcessError"
    assert report["tasks"]["error"] == "AttributeError"


def test_a_dead_session_keeps_what_needs_no_identity(marker: Path) -> None:
    report = _run_probe(marker, handle="hx_deadbeef")

    assert report["has_tasks_at_import"] is False
    assert report["files_read"] == {"ok": "'marker'"}
    assert report["shell_run"] == {"ok": "'hi'"}
    for label in ("agent_id", "agent_id_again", "context", "tasks", "agents_send"):
        assert report[label]["error"] == "HarnessAttachError", label
    assert "no harness session matches this handle" in report["agent_id"]["message"]
    assert "stop retrying and report this failure" in report["agent_id"]["message"]
    assert report["agent_id"]["message"] == report["agent_id_again"]["message"]


def test_a_live_registration_attaches_on_the_first_identity_read(marker: Path) -> None:
    store = default_store()
    agent_id = 4242
    if store.load(agent_id) is not None:
        pytest.skip("a real harness registration already owns this probe's agent id")
    handle = new_handle()
    store.save(
        HarnessRegistration(
            agent_id=agent_id,
            harness="deepseek",
            external_session_id="sess-first-read",
            machine=os.environ.get("MEIVA_MACHINE_NAME", "probe-machine"),
            controller_pid=os.getpid(),
            handle_sha256=handle_digest(handle),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        )
    )
    try:
        report = _run_probe(marker, handle=handle)
    finally:
        store.delete(agent_id)

    assert report["has_tasks_at_import"] is False, "import must not replay the state snapshot"
    assert "error" not in report["agent_id"] or report["agent_id"]["error"] == "HarnessAttachError"
    assert "error" not in report["tasks"] or report["tasks"]["error"] == "HarnessAttachError"
