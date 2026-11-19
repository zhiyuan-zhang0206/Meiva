from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from base.agents.birth.harness import HarnessProbe, probe_local_harnesses
from base.agents.birth.harness_model_probe import probe_cli_models
from base.agents.model.harness_models import CatalogProbeResult
from base.api_contracts.agent_contract import Harness, ProbedHarnessModel

_BIN_DIRS = (".local/bin", "bin")
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _external_cli(home: Path, relative_dir: str, name: str) -> Path:
    binary = home / relative_dir / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    _ = binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _stub_model_prober(
    harness: Harness,
    binary: str,
    *,
    home: Path,
    environment: dict[str, str],
    agy_stdout: str | None = None,
) -> CatalogProbeResult:
    del harness, binary, home, environment, agy_stdout
    model = ProbedHarnessModel(
        id="stub-model",
        reasoning_effort_options=("low", "high"),
        reasoning_effort_default="high",
    )
    return CatalogProbeResult(models=(model,))


def test_antigravity_retries_once_after_timeout_when_token_refreshes_on_first_call(
    tmp_path: Path,
) -> None:
    _ = _external_cli(tmp_path, ".local/bin", "agy")
    token_path = tmp_path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    _ = token_path.write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                    "expiry": (_NOW + timedelta(minutes=-5)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[float] = []

    def refreshing_cli(
        argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, env
        calls.append(timeout)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        return subprocess.CompletedProcess(
            argv, 0, stdout="gemini-4-argon-high\tGemini 4 Argon (High)\n", stderr=""
        )

    rows = probe_local_harnesses(
        tmp_path,
        str(tmp_path / "empty-bin"),
        _BIN_DIRS,
        now=_NOW,
        environ={"HOME": str(tmp_path)},
        cli_runner=refreshing_cli,
        model_prober=probe_cli_models,
    )
    row = {candidate.harness: candidate for candidate in rows}[Harness.ANTIGRAVITY]

    assert len(calls) == 2
    assert calls[0] >= 18.0
    assert row.status == "available"
    assert row.models == (
        ProbedHarnessModel(
            id="gemini-4-argon",
            reasoning_effort_options=("high",),
            reasoning_effort_default="high",
        ),
    )


def test_probe_preserves_environ_home_path_and_auth_dirs_while_stripping_secrets(
    tmp_path: Path,
) -> None:
    custom_bin = tmp_path / "custom-bin"
    _ = _external_cli(tmp_path, "custom-bin", "claude")
    _ = _external_cli(tmp_path, ".local/bin", "codex")
    claude_dir = tmp_path / "custom-claude"
    claude_dir.mkdir()
    _ = (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "fixture-access",
                    "refreshToken": "fixture-refresh",
                    "expiresAt": int((_NOW + timedelta(hours=4)).timestamp() * 1000),
                    "refreshTokenExpiresAt": int((_NOW + timedelta(days=30)).timestamp() * 1000),
                }
            }
        ),
        encoding="utf-8",
    )
    seen_envs: dict[str, dict[str, str]] = {}

    def inspecting_runner(
        argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, timeout
        seen_envs[Path(argv[0]).name] = env
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    rows: list[HarnessProbe] = probe_local_harnesses(
        now=_NOW,
        environ={
            "HOME": str(tmp_path),
            "PATH": str(custom_bin),
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(tmp_path / "custom-codex"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "ANTHROPIC_API_KEY": "fixture-ant-secret",
            "OPENAI_API_KEY": "fixture-openai-secret",
        },
        cli_runner=inspecting_runner,
        model_prober=_stub_model_prober,
    )
    by_harness = {row.harness: row for row in rows}
    claude_row = by_harness[Harness.CLAUDE_CODE]
    assert claude_row.status == "available"
    assert claude_row.on_path is True
    assert claude_row.binary == str(custom_bin / "claude")

    claude_env = seen_envs["claude"]
    assert claude_env["HOME"] == str(tmp_path)
    assert claude_env["CLAUDE_CONFIG_DIR"] == str(claude_dir)
    assert claude_env["CODEX_HOME"] == str(tmp_path / "custom-codex")
    assert claude_env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
    assert str(custom_bin) in claude_env["PATH"].split(":")
    assert str(tmp_path / ".local" / "bin") in claude_env["PATH"].split(":")
    assert "ANTHROPIC_API_KEY" not in claude_env
    assert "OPENAI_API_KEY" not in claude_env


def _install_codex_app_server_fixture(binary: Path, *, mode: str) -> None:
    _ = binary.write_text(
        f"#!{sys.executable}\n"
        + f"MODE = {mode!r}\n"
        + """\
import json
import sys

if sys.argv[1:] == ["login", "status"]:
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)

assert sys.argv[1:] == ["app-server", "--listen", "stdio://"]
init = json.loads(sys.stdin.buffer.readline())
print(json.dumps({"id": init["id"], "result": {}}), flush=True)
assert json.loads(sys.stdin.buffer.readline())["method"] == "initialized"

for raw in sys.stdin.buffer:
    req = json.loads(raw)
    method = req["method"]
    if method == "account/read":
        assert req["params"] == {"refreshToken": False}
        if MODE == "reused_refresh_token":
            print(
                "2026-11-19T07:33:09Z ERROR codex_login::auth::manager: Failed to refresh token: "
                "Your access token could not be refreshed because your refresh token was already used. "
                "Please log out and sign in again.",
                file=sys.stderr,
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "id": req["id"],
                        "result": {"account": None, "requiresOpenaiAuth": True},
                    }
                ),
                flush=True,
            )
        else:
            print(
                json.dumps(
                    {
                        "id": req["id"],
                        "result": {
                            "account": {
                                "type": "chatgpt",
                                "email": "u@example.com",
                                "planType": "pro",
                            },
                            "requiresOpenaiAuth": True,
                        },
                    }
                ),
                flush=True,
            )
    elif method == "model/list":
        print(
            json.dumps(
                {
                    "id": req["id"],
                    "result": {
                        "data": [
                            {
                                "id": "gpt-6.1-sol",
                                "isDefault": True,
                                "defaultReasoningEffort": "medium",
                                "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                            }
                        ]
                    },
                }
            ),
            flush=True,
        )
""",
        encoding="utf-8",
    )


def test_codex_probe_marks_reused_refresh_token_unavailable_even_when_login_status_passes(
    tmp_path: Path,
) -> None:
    binary = _external_cli(tmp_path, "bin", "codex")
    _install_codex_app_server_fixture(binary, mode="reused_refresh_token")

    rows = probe_local_harnesses(
        tmp_path,
        str(binary.parent),
        _BIN_DIRS,
        now=_NOW,
        environ={"HOME": str(tmp_path)},
        cli_runner=subprocess.run,
        model_prober=probe_cli_models,
    )
    row = {candidate.harness: candidate for candidate in rows}[Harness.CODEX]

    assert row.status == "unavailable"
    assert row.models == ()
    assert "refresh token was already used" in row.reason


def test_codex_probe_reports_available_without_proactive_refresh_when_account_is_live(
    tmp_path: Path,
) -> None:
    binary = _external_cli(tmp_path, "bin", "codex")
    _install_codex_app_server_fixture(binary, mode="live")

    rows = probe_local_harnesses(
        tmp_path,
        str(binary.parent),
        _BIN_DIRS,
        now=_NOW,
        environ={"HOME": str(tmp_path)},
        cli_runner=subprocess.run,
        model_prober=probe_cli_models,
    )
    row = {candidate.harness: candidate for candidate in rows}[Harness.CODEX]

    assert row.status == "available"
    assert [model.id for model in row.models] == ["gpt-6.1-sol"]
