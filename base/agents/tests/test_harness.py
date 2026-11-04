from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from base.agents.birth.harness import (
    LAUNCHABLE_HARNESSES,
    HarnessProbe,
    coerce_harness,
    probe_local_harnesses,
    resolve_birth_harness,
)
from base.agents.birth.harness_model_probe import probe_cli_models
from base.agents.model.harness_models import CatalogProbeResult
from base.api_contracts.agent_contract import (
    DEFAULT_HARNESS,
    ErrorReason,
    ForkHarnessMismatch,
    Harness,
    HarnessNotLaunchable,
    MeivaAgentError,
    ProbedHarnessModel,
)

_BIN_DIRS = (".local/bin", "bin")

# meiva:note
# The instant the probe is asked at; every credential these tests write is dated against it.
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _external_cli(home: Path, relative_dir: str, name: str) -> Path:
    binary = home / relative_dir / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _iso(offset: timedelta) -> str:
    return (_NOW + offset).isoformat()


def _epoch_ms(offset: timedelta) -> int:
    return int((_NOW + offset).timestamp() * 1000)


def _claude_credentials(home: Path, *, access: timedelta, refresh: timedelta) -> None:
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access",
                    "refreshToken": "refresh",
                    "expiresAt": _epoch_ms(access),
                    "refreshTokenExpiresAt": _epoch_ms(refresh),
                }
            }
        ),
        encoding="utf-8",
    )


def _antigravity_token(home: Path, *, relative: str, expiry: timedelta) -> None:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    token = {"access_token": "access", "refresh_token": "renewable", "expiry": _iso(expiry)}
    path.write_text(json.dumps({"token": token}), encoding="utf-8")


# meiva:note
# The stub CLIs these tests install print nothing and exit 1, which is "the CLI could not
# tell us" — the row then falls back to the stored credential, which is what the older
# assertions describe. A test about what the CLI itself answers passes its own runner.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def _silent_cli(
    argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
) -> Any:
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")


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
    return CatalogProbeResult(models=(model,), default_model="stub-model")


def _probe(
    home: Path,
    *,
    cli_runner: Any = _silent_cli,
    model_prober: Any = _stub_model_prober,
) -> dict[Harness, HarnessProbe]:
    rows = probe_local_harnesses(
        home,
        str(home / "empty-bin"),
        _BIN_DIRS,
        now=_NOW,
        environ={},
        cli_runner=cli_runner,
        model_prober=model_prober,
    )
    return {row.harness: row for row in rows}


def test_every_harness_gets_one_readable_row(tmp_path: Path) -> None:
    rows = probe_local_harnesses(
        tmp_path, str(tmp_path / "empty-bin"), _BIN_DIRS, now=_NOW, environ={}
    )
    assert [row.harness for row in rows] == list(Harness)
    assert all(row.reason for row in rows)


def test_the_builtin_runtime_needs_no_external_command(tmp_path: Path) -> None:
    row = _probe(tmp_path)[Harness.MEIVA]
    assert row.status == "available"
    assert row.binary is None


def test_a_missing_command_is_unavailable_and_names_what_was_searched(tmp_path: Path) -> None:
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "unavailable"
    assert "agy" in row.reason


def test_an_installed_signed_in_harness_is_available(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=8), refresh=timedelta(days=30))
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "available"
    assert row.binary == str(tmp_path / ".local" / "bin" / "claude")
    assert row.on_path is False


def test_a_recorded_account_without_a_token_is_unknown_not_available(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "account"}}), encoding="utf-8"
    )
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "unknown"
    assert "stores no token" in row.reason


def test_an_expired_refresh_token_is_unavailable(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=8), refresh=timedelta(days=-1))
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "unavailable"
    assert "expired at" in row.reason


def test_an_expired_access_token_is_a_pending_renewal_not_a_logout(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=-1), refresh=timedelta(days=10))
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "available"
    assert "valid until" in row.reason


def test_a_codex_token_set_is_available_and_reports_its_age(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "codex")
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps(
            {
                "tokens": {"access_token": "access", "refresh_token": "refresh"},
                "last_refresh": "2026-10-18T12:19:55Z",
            }
        ),
        encoding="utf-8",
    )
    row = _probe(tmp_path)[Harness.CODEX]
    assert row.status == "available"
    assert "last refreshed 2026-10-18T12:19:55Z" in row.reason


def test_an_antigravity_session_reports_its_access_expiry_as_age(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    _antigravity_token(
        tmp_path,
        relative=".gemini/antigravity-cli/antigravity-oauth-token",
        expiry=timedelta(hours=-1),
    )
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "available"
    assert "renewed on use" in row.reason


def test_an_ide_session_is_not_a_cli_login(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    _antigravity_token(
        tmp_path, relative=".gemini/jetski-standalone-oauth-token", expiry=timedelta(hours=1)
    )
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "unknown"
    assert "antigravity-cli/antigravity-oauth-token" in row.reason


def test_a_path_install_is_reported_as_on_path(tmp_path: Path) -> None:
    binary = _external_cli(tmp_path, "bin", "codex")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "token"}}), encoding="utf-8"
    )
    rows = probe_local_harnesses(
        tmp_path,
        str(binary.parent),
        _BIN_DIRS,
        now=_NOW,
        environ={},
        cli_runner=_silent_cli,
        model_prober=_stub_model_prober,
    )
    row = {candidate.harness: candidate for candidate in rows}[Harness.CODEX]
    assert row.status == "available"
    assert row.on_path is True


def test_an_installed_but_unsigned_harness_is_unavailable(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "unavailable"
    assert "not signed in" in row.reason


def test_an_unreadable_sign_in_state_is_unknown_not_a_guess(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "unknown"
    assert row.binary == str(tmp_path / ".local" / "bin" / "agy")
    assert "unknown" in row.reason


def _answering_cli(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    def runner(
        argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
    ) -> Any:
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return runner


def _silent_after_timeout() -> Any:
    def runner(
        argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
    ) -> Any:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    return runner


# meiva:note
# The machine's answer comes from its CLI, not from a file an installer may not have
# written: macbook-air runs Claude Code with no ~/.claude/.credentials.json, and asking the
# file would report a working machine as unknown — which now blocks the spawn (#5498).
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def test_the_cli_answer_is_available_without_any_credentials_file(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")

    row = _probe(
        tmp_path,
        cli_runner=_answering_cli(stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"})),
    )[Harness.CLAUDE_CODE]

    assert row.status == "available"
    assert "claude auth status" in row.reason


def test_the_cli_answer_outranks_a_stored_token(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=1), refresh=timedelta(days=1))

    row = _probe(tmp_path, cli_runner=_answering_cli(stdout=json.dumps({"loggedIn": False})))[
        Harness.CLAUDE_CODE
    ]

    assert row.status == "unavailable"
    assert "no login" in row.reason


def test_a_codex_login_answer_needs_no_auth_file(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "codex")

    row = _probe(tmp_path, cli_runner=_answering_cli(stderr="Logged in using ChatGPT"))[
        Harness.CODEX
    ]

    assert row.status == "available"
    assert "codex login status" in row.reason


def test_an_antigravity_catalogue_means_a_login(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")

    row = _probe(
        tmp_path,
        cli_runner=_answering_cli(stdout="gemini-4-argon-high\tGemini 4 Argon (High)\n"),
        model_prober=probe_cli_models,
    )[Harness.ANTIGRAVITY]

    assert row.status == "available"
    assert "agy models" in row.reason
    assert row.default_model == "gemini-4-argon"
    assert row.models == (
        ProbedHarnessModel(
            id="gemini-4-argon",
            reasoning_effort_options=("high",),
            reasoning_effort_default="high",
        ),
    )


def test_a_signed_in_harness_whose_catalogue_probe_fails_is_unknown(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "codex")

    def failing_prober(*_args: object, **_kwargs: object) -> CatalogProbeResult:
        return CatalogProbeResult(error="codex app-server timed out")

    row = _probe(
        tmp_path,
        cli_runner=_answering_cli(stderr="Logged in using ChatGPT"),
        model_prober=failing_prober,
    )[Harness.CODEX]

    assert row.status == "unknown"
    assert row.models == ()
    assert "codex login status" in row.reason
    assert "model catalogue probe failed: codex app-server timed out" in row.reason


def test_an_antigravity_stored_token_without_catalogue_stdout_is_unknown(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    _antigravity_token(
        tmp_path,
        relative=".gemini/antigravity-cli/antigravity-oauth-token",
        expiry=timedelta(hours=1),
    )

    row = _probe(tmp_path, cli_runner=_silent_after_timeout(), model_prober=probe_cli_models)[
        Harness.ANTIGRAVITY
    ]

    assert row.status == "unknown"
    assert "model catalogue probe failed" in row.reason


def test_an_antigravity_sign_in_request_is_unavailable(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")

    row = _probe(
        tmp_path,
        cli_runner=_answering_cli(
            stderr="Error: Please sign in to view available models.", returncode=1
        ),
    )[Harness.ANTIGRAVITY]

    assert row.status == "unavailable"
    assert "sign-in" in row.reason


def test_a_cli_that_cannot_answer_falls_back_to_the_stored_credential(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=1), refresh=timedelta(days=1))

    row = _probe(tmp_path, cli_runner=_silent_after_timeout())[Harness.CLAUDE_CODE]

    assert row.status == "available"
    assert ".claude/.credentials.json" in row.reason


def test_a_cli_that_cannot_answer_with_no_credential_stays_unknown(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")

    row = _probe(tmp_path, cli_runner=_silent_after_timeout())[Harness.ANTIGRAVITY]

    assert row.status == "unknown"


def test_coerce_harness_accepts_the_enum_and_refuses_unknown_names() -> None:
    assert coerce_harness(None) is DEFAULT_HARNESS
    assert coerce_harness("codex") is Harness.CODEX
    assert coerce_harness(Harness.ANTIGRAVITY) is Harness.ANTIGRAVITY
    with pytest.raises(ValueError, match="unknown harness") as refusal:
        coerce_harness("claude-code")
    assert "claude_code" in str(refusal.value)
    with pytest.raises(ValueError, match="unknown harness"):
        coerce_harness("kimi_code")


def test_fork_harness_mismatch_is_a_fork_error() -> None:
    from base.api_contracts.agent_contract import ForkError, MeivaAgentError

    assert issubclass(ForkHarnessMismatch, ForkError)
    assert issubclass(ForkHarnessMismatch, MeivaAgentError)
    assert issubclass(ForkHarnessMismatch, ValueError)


class _ForkSourceCursor:
    def __init__(self, harness: str) -> None:
        self._harness = harness

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchone(self) -> tuple[str]:
        return (self._harness,)


def test_resolve_birth_harness_refuses_forking_an_external_harness_source() -> None:
    cur = cast("psycopg.Cursor[Any]", _ForkSourceCursor("claude_code"))
    with pytest.raises(ForkHarnessMismatch, match="only supported on the native 'meiva' harness"):
        resolve_birth_harness(cur, None, 17)


def test_resolve_birth_harness_refuses_forking_onto_an_external_harness() -> None:
    cur = cast("psycopg.Cursor[Any]", _ForkSourceCursor("meiva"))
    with pytest.raises(ForkHarnessMismatch, match="cannot fork agent 17 onto harness 'codex'"):
        resolve_birth_harness(cur, Harness.CODEX, 17)


class _UnreadCursor:
    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("a plain birth reads no fork source")


def _no_cursor() -> psycopg.Cursor[Any]:
    return cast("psycopg.Cursor[Any]", _UnreadCursor())


# meiva:note
# Every declared harness has a launcher today, so the refusal is driven by handing
# the rule a narrower set: membership is what decides, and this pins the moment a
# launcher is retired or a harness is declared before one.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def test_a_harness_outside_the_launchable_set_is_refused() -> None:
    with pytest.raises(HarnessNotLaunchable) as refusal:
        resolve_birth_harness(
            _no_cursor(), Harness.CODEX, None, launchable=frozenset({Harness.MEIVA})
        )
    detail = str(refusal.value)
    assert "codex" in detail
    assert "meiva" in detail
    assert "claude_code" not in detail


def test_the_shipped_set_is_what_the_birth_gate_reads() -> None:
    for launchable in LAUNCHABLE_HARNESSES:
        assert resolve_birth_harness(_no_cursor(), launchable, None) == launchable.value


def test_every_launchable_harness_is_accepted_at_birth() -> None:
    for launchable in LAUNCHABLE_HARNESSES:
        assert resolve_birth_harness(_no_cursor(), launchable, None) == launchable.value


def test_harness_not_launchable_is_a_wire_error() -> None:
    assert issubclass(HarnessNotLaunchable, MeivaAgentError)
    assert HarnessNotLaunchable.reason is ErrorReason.HARNESS_NOT_LAUNCHABLE
    assert HarnessNotLaunchable.http_status == 400


def test_codex_model_probe_keeps_stdin_open_for_model_list(tmp_path: Path) -> None:
    binary = _external_cli(tmp_path, "bin", "codex")
    _ = binary.write_text(
        f"#!{sys.executable}\n"
        + """\
import json
import os
import select
import sys

assert len(sys.argv) == 4 and sys.argv[1:] == ["app-server", "--listen", "stdio://"]
assert "OPENAI_API_KEY" not in os.environ
init = json.loads(sys.stdin.buffer.readline())
assert init["method"] == "initialize"
print(json.dumps({"id": init["id"], "result": {}}), flush=True)
assert json.loads(sys.stdin.buffer.readline())["method"] == "initialized"
request = json.loads(sys.stdin.buffer.readline())
assert request["method"] == "model/list"
assert not select.select([sys.stdin], [], [], 0)[0], "stdin closed before model/list reply"
print(json.dumps({"id": request["id"], "result": {"data": [{
    "id": "gpt-6.1-terra",
    "isDefault": True,
    "defaultReasoningEffort": "low",
    "supportedReasoningEfforts": [
        {"reasoningEffort": "low"},
        {"reasoningEffort": "ultra"},
    ],
}]}}), flush=True)
sys.stdin.buffer.read()
""",
        encoding="utf-8",
    )

    result = probe_cli_models(
        Harness.CODEX,
        str(binary),
        home=tmp_path,
        environment={"OPENAI_API_KEY": "sk-native", "PATH": "/usr/local/bin"},
    )

    assert result.error is None
    assert result.default_model == "gpt-6.1-terra"
    assert result.models == (
        ProbedHarnessModel(
            id="gpt-6.1-terra",
            reasoning_effort_options=("low", "ultra"),
            reasoning_effort_default="low",
        ),
    )


def test_probe_cli_models_claude_blanks_secret_env_and_reads_initialize(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        reply = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "response": {
                    "models": [
                        {
                            "value": "default",
                            "resolvedModel": "claude-opus-4-6",
                            "supportsEffort": True,
                            "supportedEffortLevels": ["low", "medium", "high"],
                            "defaultEffort": "high",
                        }
                    ]
                },
            },
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(reply) + "\n", stderr="")

    result = probe_cli_models(
        Harness.CLAUDE_CODE,
        "/usr/local/bin/claude",
        home=tmp_path,
        environment={"ANTHROPIC_API_KEY": "sk-ant", "CLAUDECODE": "1", "PATH": "/usr/local/bin"},
        runner=fake_runner,
    )
    assert captured["env"]["ANTHROPIC_API_KEY"] == ""
    assert "CLAUDECODE" not in captured["env"]
    assert result.default_model == "claude-opus-4-6"
    assert result.models == (
        ProbedHarnessModel(
            id="claude-opus-4-6",
            reasoning_effort_options=("low", "medium", "high"),
            reasoning_effort_default="high",
        ),
    )


# meiva:note
# The probe runs the CLI under `child_env`, which blanks every credential, so its "no login" answer
# describes the probe's environment rather than the launcher's. When this process holds a credential
# the probe strips, the honest row is `unknown` — never `available`, and no longer `unavailable`
# (#6150: a claude_code agent ran a real turn with a key the probe had already called absent).
def test_a_credential_the_probe_cannot_see_is_unknown_not_a_logout(tmp_path: Path) -> None:
    _external_cli(tmp_path, "bin", "claude")

    def runner(
        argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
    ) -> Any:
        assert "ANTHROPIC_API_KEY" not in env, "the probe is supposed to run without the credential"
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"loggedIn": False}), stderr=""
        )

    rows = probe_local_harnesses(
        tmp_path,
        str(tmp_path / "empty-bin"),
        _BIN_DIRS,
        now=_NOW,
        environ={"ANTHROPIC_API_KEY": "sk-not-forwarded"},
        cli_runner=runner,
        model_prober=_stub_model_prober,
    )
    row = {candidate.harness: candidate for candidate in rows}[Harness.CLAUDE_CODE]

    assert row.status == "unknown"
    assert "ANTHROPIC_API_KEY" in row.reason
    assert "not signed in" not in row.reason


# meiva:note
# The positive control: the same CLI answer with nothing withheld is still a logout, so the rule
# above narrows one row rather than weakening every probe.
def test_the_same_answer_without_a_withheld_credential_is_still_unavailable(tmp_path: Path) -> None:
    _external_cli(tmp_path, "bin", "claude")

    def runner(
        argv: list[str], *, capture_output: bool, text: bool, timeout: float, env: dict[str, str]
    ) -> Any:
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"loggedIn": False}), stderr=""
        )

    rows = probe_local_harnesses(
        tmp_path,
        str(tmp_path / "empty-bin"),
        _BIN_DIRS,
        now=_NOW,
        environ={},
        cli_runner=runner,
        model_prober=_stub_model_prober,
    )
    row = {candidate.harness: candidate for candidate in rows}[Harness.CLAUDE_CODE]

    assert row.status == "unavailable"
    assert "not signed in" in row.reason
