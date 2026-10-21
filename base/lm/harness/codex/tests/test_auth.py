from __future__ import annotations

from pathlib import Path
from typing import cast

from base.lm.harness.codex.auth import SECRET_ENV_NAMES, build_env
from base.lm.harness.codex.config import CodexConfig
from base.lm.harness.codex.session import CodexSession
from base.lm.harness.codex.transport import StdioAppServer

SAMPLE_KEY = "sk-proj-sample-value"


# meiva:note
# A session resolves the CLI it will spawn before it builds the transport, so a test that
# constructs one on a machine without `codex` pins its own stand-in: what these tests
# assert is the environment the transport is handed, not where the binary lives.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def _stub_codex(root: Path) -> str:
    stub = root / "codex"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


def test_the_key_never_reaches_the_child_by_default() -> None:
    env = build_env({"OPENAI_API_KEY": SAMPLE_KEY, "PATH": "/bin", "CODEX_HOME": "/h"})
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "/bin", "the rest of the caller's environment view rides along"
    assert env["CODEX_HOME"] == "/h"


def test_every_credential_variable_the_cli_reads_is_covered() -> None:
    env = build_env(dict.fromkeys(SECRET_ENV_NAMES, SAMPLE_KEY))
    assert env == {}


def test_api_key_mode_passes_the_key_through() -> None:
    env = build_env({"OPENAI_API_KEY": SAMPLE_KEY}, api_key_mode=True)
    assert env["OPENAI_API_KEY"] == SAMPLE_KEY


# meiva:note
# The wiring hands the session the inherited view (environ=None on the agent path), where
# the child would otherwise take the process' own key and bill per token; the session
# sanitizes whatever view it is handed before the transport spawns the CLI.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
def test_a_session_sanitizes_the_environment_it_hands_the_transport(tmp_path: Path) -> None:
    session = CodexSession(
        CodexConfig(cwd=tmp_path, binary=_stub_codex(tmp_path)),
        environ={"OPENAI_API_KEY": SAMPLE_KEY, "PATH": "/bin"},
    )
    assert cast(StdioAppServer, session._transport)._environ == {"PATH": "/bin"}


def test_api_key_mode_reaches_the_child_through_the_session(tmp_path: Path) -> None:
    session = CodexSession(
        CodexConfig(cwd=tmp_path, api_key_mode=True, binary=_stub_codex(tmp_path)),
        environ={"OPENAI_API_KEY": SAMPLE_KEY},
    )
    assert cast(StdioAppServer, session._transport)._environ == {"OPENAI_API_KEY": SAMPLE_KEY}
