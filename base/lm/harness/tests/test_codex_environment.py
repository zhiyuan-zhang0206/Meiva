from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness import HarnessClosedError, HarnessTurnFaultedError
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.codex import CodexConfig
from base.lm.harness.codex.auth import SECRET_ENV_NAMES, build_env
from base.lm.harness.codex.session import CodexSession
from base.lm.harness.codex.tests import child_environment_server
from base.lm.harness.registration import HANDLE_ENV
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.tests.bridge_support import run_turn
from base.lm.harness.tests.roots import bridge_root_values
from base.lm.harness.wiring import HarnessOtlp, bridge_for, open_session

SAMPLE_KEY = "sk-proj-sample-value"


# meiva:note
# The executable is a local stdio peer, reached through the same resolved binary
# and real subprocess transport as Codex. No installed Codex CLI is invoked.
# meiva:link base/lm/harness/docs/harness.md
def _stub_codex(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    stub = root / "codex"
    peer = Path(child_environment_server.__file__)
    stub.write_text(f"#!{sys.executable}\n" + peer.read_text())
    stub.chmod(0o755)
    return str(stub)


# meiva:note
# Credentials are removed, including empty values, unless API-key billing was
# explicitly requested. Filtering and copying happen at construction, before
# start; mutation of the caller's mapping must not change that child's authority.
# meiva:link base/lm/harness/codex/docs/codex.md
@pytest.mark.parametrize("api_key_mode", [False, True])
async def test_session_snapshots_the_environment_received_by_its_process_tree(
    tmp_path: Path, api_key_mode: bool
) -> None:
    receipt = tmp_path / "environment.json"
    environment = {
        "OPENAI_API_KEY": SAMPLE_KEY,
        "CODEX_API_KEY": "",
        "CODEX_ACCESS_TOKEN": "test-token",
        "HOME": str(tmp_path / "home"),
        "MEIVA_HOME": str(tmp_path / "meiva"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "PATH": "/bin",
        HANDLE_ENV: "test-registration",
        "MEIVA_TEST_ENV_RECEIPT": str(receipt),
    }
    original = dict(environment)
    session = CodexSession(
        CodexConfig(cwd=tmp_path, binary=_stub_codex(tmp_path), api_key_mode=api_key_mode),
        environ=environment,
        binding=SessionBinding(),
        clock=system_clock(),
    )
    assert session.cli_pid is None
    assert not receipt.exists(), "cold construction must not spawn the peer"
    environment.clear()
    environment["OPENAI_API_KEY"] = "later-key"
    try:
        await session.start()
        observed = json.loads(receipt.read_text())
        expected = build_env(original, api_key_mode=api_key_mode)
        for process in (observed["environment"], observed["descendant"]):
            assert {name: process[name] for name in expected} == expected
            assert "PYTEST_CURRENT_TEST" not in process
            if not api_key_mode:
                assert not set(SECRET_ENV_NAMES).intersection(process)
        assert observed["cwd"] == str(tmp_path)
    finally:
        await session.close()
    assert session.exit_code is not None


def _assert_resumed_child_environment(
    first: dict[str, str], resumed: dict[str, str], descendant: dict[str, str]
) -> None:
    assert resumed["SESSION_VALUE"] == "resumed-session"
    assert resumed[HANDLE_ENV] != first[HANDLE_ENV]
    assert descendant[HANDLE_ENV] == resumed[HANDLE_ENV]
    assert "OPENAI_API_KEY" not in descendant


async def test_bridge_recaptures_environment_and_registration_when_resuming(tmp_path: Path) -> None:
    binary = Path(_stub_codex(tmp_path / "bin"))
    receipt = tmp_path / "environment.json"
    environment = {
        "HOME": str(tmp_path),
        "PATH": str(binary.parent),
        "OPENAI_API_KEY": SAMPLE_KEY,
        "MEIVA_TEST_ENV_RECEIPT": str(receipt),
        "SESSION_VALUE": "before-first-session",
    }
    bridge = bridge_for(
        Harness.CODEX,
        agent_id=7,
        machine="local-environment-test",
        workspace=tmp_path,
        home=tmp_path / "home",
        registry=FileSessionRegistry(tmp_path / "registry"),
        registration_ttl_seconds=60,
        environ=environment,
        root=bridge_root_values(),
    )
    assert bridge is not None
    assert not receipt.exists()
    environment["SESSION_VALUE"] = "first-session"
    try:
        await bridge.ensure()
        first = json.loads(receipt.read_text())
        assert first["method"] == "thread/start"
        assert first["environment"]["SESSION_VALUE"] == "first-session"
        assert str(binary.parent) in first["environment"]["PATH"].split(":")
        assert "OPENAI_API_KEY" not in first["environment"]
        assert bridge.exit_code is None
        with pytest.raises((HarnessClosedError, HarnessTurnFaultedError)):
            await run_turn(bridge, "unsupported by local peer", key="retire-peer")
        assert bridge.exit_code is not None
        environment["SESSION_VALUE"] = "resumed-session"
        resumed = await bridge.ensure()
        second = json.loads(receipt.read_text())
        assert second["method"] == "thread/resume"
        assert resumed.session_id == "environment-thread"
        assert bridge.exit_code is None
        _assert_resumed_child_environment(
            first["environment"], second["environment"], second["descendant"]
        )
    finally:
        await bridge.close()
    assert bridge.exit_code is not None


@pytest.mark.parametrize(
    ("proxy_env", "expected"),
    [
        ({}, {"NO_PROXY": "127.0.0.1"}),
        ({"NO_PROXY": "127.0.0.2"}, {"NO_PROXY": "127.0.0.2,127.0.0.1"}),
        ({"no_proxy": ".test.invalid"}, {"no_proxy": ".test.invalid,127.0.0.1"}),
        (
            {"NO_PROXY": "127.0.0.2", "no_proxy": ".test.invalid"},
            {"NO_PROXY": "127.0.0.2,127.0.0.1", "no_proxy": ".test.invalid,127.0.0.1"},
        ),
        ({"NO_PROXY": "*"}, {"NO_PROXY": "*"}),
        (
            {"NO_PROXY": "*", "no_proxy": ".test.invalid"},
            {"NO_PROXY": "*", "no_proxy": ".test.invalid,127.0.0.1"},
        ),
        ({"NO_PROXY": "*", "no_proxy": ""}, {"NO_PROXY": "*", "no_proxy": ""}),
    ],
)
async def test_native_otlp_preserves_proxy_exceptions(
    tmp_path: Path, proxy_env: dict[str, str], expected: dict[str, str]
) -> None:
    binary = Path(_stub_codex(tmp_path / "bin"))
    receipt = tmp_path / "environment.json"
    session = open_session(
        Harness.CODEX,
        agent_id=7,
        cwd=tmp_path,
        registry=FileSessionRegistry(tmp_path / "registry"),
        environ={
            "PATH": str(binary.parent),
            "HOME": str(tmp_path),
            "MEIVA_TEST_ENV_RECEIPT": str(receipt),
            **proxy_env,
        },
        clock=system_clock(),
        otlp_endpoint="http://127.0.0.1:20702/meiva-harness",
    )
    try:
        await session.start()
        observed = json.loads(receipt.read_text())
        for child in (observed["environment"], observed["descendant"]):
            assert {key: child[key] for key in ("NO_PROXY", "no_proxy") if key in child} == expected
    finally:
        await session.close()


@pytest.mark.parametrize(
    ("mode", "expected"), [("normal", True), ("large", False), ("drip", False)]
)
async def test_native_otlp_probe_bounds_total_read_and_response_size(
    mode: str, expected: bool
) -> None:
    finished = asyncio.Event()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            _ = await reader.readuntil(b"\r\n\r\n")
            length = 2 if mode == "normal" else 17
            writer.write(
                f"HTTP/1.1 200 OK\r\nX-Meiva-Harness-Redaction: 1\r\n"
                f"Content-Length: {length}\r\n\r\n".encode()
            )
            if mode == "drip":
                for _ in range(length):
                    await asyncio.sleep(0.1)
                    writer.write(b"x")
                    await writer.drain()
            else:
                writer.write(b"x" * length)
                await writer.drain()
        except ConnectionError:
            return
        finally:
            writer.close()
            finished.set()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        addr = server.sockets[0].getsockname()
        telemetry = HarnessOtlp(f"http://127.0.0.1:{addr[1]}/meiva-harness", True)
        async with asyncio.timeout(1.4):
            assert await telemetry.available() is expected
        await asyncio.wait_for(finished.wait(), 2)
