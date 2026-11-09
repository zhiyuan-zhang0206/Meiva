import os
import re
import sys
from pathlib import Path

from base.host.net.tests.local_http import closed_port_url
from base.host.proc import run_bounded
from meiva.sdk_surface import process_context

_CHILD = Path(__file__).with_name("child_sdk_probes.py")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_non_agent_import_keeps_existing_unbound_context_contract() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD), "non_agent_import_unbound_context"],
        cwd=process_context.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _clean_outage_env(home: Path, unreachable: str, canary: str) -> dict[str, str]:
    env = {
        **os.environ,
        "MEIVA_HOME": str(home),
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "MEIVA_MACHINE_SERVE_GATEWAY": "false",
        "MEIVA_GATEWAY_URL": unreachable,
        "MEIVA_API_TOKEN": canary,
        "MEIVA_CONFIG_FETCH": "skip",
    }
    for stripped in ("MEIVA_AGENT_ID", "MEIVA_HARNESS_HANDLE", "MEIVA_CONFIG_SOURCE"):
        _ = env.pop(stripped, None)
    return env


# meiva:note
# Simulate a bare `import meiva` tool subprocess (`child_sdk_probes.py`, fixed argv) whose
# background sampling refresh hits an unreachable gateway while logging onto default stderr handler
# 0 (task #6594).
def test_bare_import_meiva_subprocess_outage_never_exposes_bearer_token(tmp_path: Path) -> None:
    canary = "canary-fake-bearer-token-998877"
    home = tmp_path / "home"
    home.mkdir()
    _ = (home / ".env").write_text("", encoding="utf-8")
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD), "bare_import_meiva_refresh_outage"],
        cwd=process_context.loaded_code_image().source_root,
        env=_clean_outage_env(home, closed_port_url(), canary),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    plain_stderr = _ANSI.sub("", result.stderr)
    motive = (
        "a bare subprocess importing `meiva` without prepare_process_logging or add_sink must "
        "guard default stderr handler 0 (`_diagnose=False` + redaction patcher) before any SDK "
        "refresh or warning runs: on 2026-11-09 (task #6594) a gateway outage during "
        "sdk-call-policy refresh dumped stack-frame `Authorization: Bearer <token>` locals into "
        f"tool output; see meiva/boot.py, base/agents/sdk/call_policy.py, and base/log/sinks.py. stderr={plain_stderr!r}"
    )

    assert result.stdout.strip() == "diagnosing: []", motive
    assert canary not in result.stdout and canary not in result.stderr, motive
    forbidden = [t for t in ("Authorization", "Bearer", "└ ") if t in plain_stderr]
    assert forbidden == [], motive
    required = (
        "SDK sampling config fetch unavailable; retaining the last valid policy",
        "ConnectError",
        "call_policy.py",
        "bootstrap.py",
        "http_dial.py",
        "MEIVA_API_TOKEN=***",
    )
    assert [t for t in required if t not in plain_stderr] == [], motive
