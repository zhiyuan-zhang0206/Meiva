from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.codex import CodexConfig, CodexSession
from base.lm.harness.codex.auth import SECRET_ENV_NAMES, build_env
from base.lm.harness.codex.tests import child_environment_server
from base.lm.harness.registration import HANDLE_ENV
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.tests.roots import bridge_root_values
from base.lm.harness.wiring import bridge_for

SAMPLE_KEY = "sk-proj-sample-value"


# meiva:note
# The executable is a local stdio peer, reached through the same resolved binary
# and real subprocess transport as Codex. No installed Codex CLI is invoked.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
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
        await bridge.interrupt()
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
