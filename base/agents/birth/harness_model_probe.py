from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeGuard

from base.agents.model.harness_models import (
    CatalogProbeResult,
    parse_antigravity_models,
    parse_claude_initialize,
    parse_codex_model_list,
)
from base.api_contracts.agent_contract import Harness
from base.host.boundary.untyped import object_of
from base.lm.harness.claude import SECRET_ENV_NAMES as CLAUDE_SECRET_ENV_NAMES
from base.lm.harness.codex import JsonRpcError, TransportClosedError
from base.lm.harness.codex.session import list_models as list_codex_models

StdinRunner = Callable[..., "subprocess.CompletedProcess[str]"]
ModelProber = Callable[..., CatalogProbeResult]

_MODEL_PROBE_TIMEOUT_S: dict[Harness, float] = {
    Harness.CLAUDE_CODE: 12.0,
    Harness.CODEX: 10.0,
    Harness.ANTIGRAVITY: 12.0,
}


def _decode_timeout_stdout(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _is_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _json_lines(stdout: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            item = object_of(json.loads, line)
        except json.JSONDecodeError:
            continue
        if _is_dict(item):
            parsed.append(item)
    return parsed


def _run_cli(
    argv: list[str],
    *,
    stdin_payload: str | None,
    timeout: float,
    env: dict[str, str],
    runner: StdinRunner,
) -> tuple[str | None, str | None]:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
    }
    if stdin_payload is not None:
        kwargs["input"] = stdin_payload
    try:
        done = runner(argv, **kwargs)
    except subprocess.TimeoutExpired as exc:
        partial = _decode_timeout_stdout(exc.stdout)
        if partial.strip():
            return partial, f"`{' '.join(argv[:2])}` timed out after {timeout:.0f}s"
        return None, f"`{' '.join(argv[:2])}` did not answer within {timeout:.0f}s"
    except OSError as exc:
        return None, f"`{' '.join(argv[:2])}` could not run: {exc}"

    if done.returncode != 0 and not done.stdout.strip():
        stderr = done.stderr.strip().splitlines()
        summary = stderr[-1] if stderr else f"exit code {done.returncode}"
        return None, f"`{' '.join(argv[:2])}` failed: {summary}"
    return done.stdout, None


def _probe_codex_models(
    binary: str, *, home: Path, environment: Mapping[str, str]
) -> CatalogProbeResult:
    timeout = _MODEL_PROBE_TIMEOUT_S[Harness.CODEX]
    try:
        page = asyncio.run(
            list_codex_models(binary, cwd=home, environ=environment, timeout=timeout)
        )
    except TimeoutError:
        return CatalogProbeResult(
            error=f"codex app-server model probe timed out after {timeout:.0f}s"
        )
    except (OSError, JsonRpcError, TransportClosedError) as exc:
        return CatalogProbeResult(error=f"codex app-server model probe failed: {exc}")
    return parse_codex_model_list([page])


def _probe_claude_models(
    binary: str, *, environment: Mapping[str, str], runner: StdinRunner
) -> CatalogProbeResult:
    env = {key: value for key, value in environment.items() if key != "CLAUDECODE"}
    env.update(dict.fromkeys(CLAUDE_SECRET_ENV_NAMES, ""))
    stdin_payload = (
        json.dumps(
            {
                "type": "control_request",
                "request_id": "probe-init",
                "request": {"subtype": "initialize"},
            }
        )
        + "\n"
    )
    stdout, err = _run_cli(
        [
            binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
        ],
        stdin_payload=stdin_payload,
        timeout=_MODEL_PROBE_TIMEOUT_S[Harness.CLAUDE_CODE],
        env=env,
        runner=runner,
    )
    if stdout is None:
        return CatalogProbeResult(error=err or "claude initialize produced no output")
    for message in _json_lines(stdout):
        if message.get("type") == "control_response":
            return parse_claude_initialize(message)
    return CatalogProbeResult(error=err or "claude emitted no initialize control_response")


def probe_cli_models(
    harness: Harness,
    binary: str,
    *,
    home: Path,
    environment: Mapping[str, str],
    agy_stdout: str | None = None,
    runner: StdinRunner = subprocess.run,
) -> CatalogProbeResult:
    if harness is Harness.ANTIGRAVITY:
        if agy_stdout is not None:
            return parse_antigravity_models(agy_stdout)
        stdout, err = _run_cli(
            [binary, "models"],
            stdin_payload=None,
            timeout=_MODEL_PROBE_TIMEOUT_S[Harness.ANTIGRAVITY],
            env=dict(environment),
            runner=runner,
        )
        if stdout is None:
            return CatalogProbeResult(error=err or "agy models produced no output")
        return parse_antigravity_models(stdout)
    if harness is Harness.CODEX:
        return _probe_codex_models(binary, home=home, environment=environment)
    if harness is Harness.CLAUDE_CODE:
        return _probe_claude_models(binary, environment=environment, runner=runner)
    raise AssertionError(f"built-in harness {harness.value!r} has no external CLI model probe")
