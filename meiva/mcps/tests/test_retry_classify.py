from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meiva.mcps.config import is_transport_error

PROBE = Path(__file__).with_name("transport_error_probe.py")


def test_transport_subclasses_keep_the_known_retry_contract() -> None:
    from anyio import BrokenResourceError
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    class PeerDisconnectedError(BrokenResourceError):
        pass

    class SessionDisconnectedError(MCPError):
        pass

    assert is_transport_error(PeerDisconnectedError()) is True
    assert (
        is_transport_error(SessionDisconnectedError(CONNECTION_CLOSED, "Connection closed")) is True
    )


# meiva:note
# The fault is a real one: a child interpreter whose `mcp` package has no `types` module, so the lazy `from mcp.types import ...` inside the classifier fails for real. The classifier must let that ImportError out; an answer of False would silently turn a broken install into "never retry".
def test_required_dependency_failure_does_not_become_an_empty_retry_policy(
    tmp_path: Path,
) -> None:
    shadow = tmp_path / "shadow"
    (shadow / "mcp").mkdir(parents=True)
    (shadow / "mcp" / "__init__.py").write_text("class MCPError(Exception):\n    code = 0\n")

    done = subprocess.run(  # noqa: S603
        [sys.executable, str(PROBE), str(shadow)],
        env={**os.environ, "MEIVA_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "ImportError" in done.stdout
    assert "mcp.types" in done.stdout


@pytest.mark.parametrize("code", [errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED])
def test_native_transport_errno_is_retryable(code: int) -> None:
    assert is_transport_error(OSError(code, "transport unavailable")) is True


def test_unrelated_io_failure_is_not_a_transport_retry() -> None:
    assert is_transport_error(PermissionError(errno.EACCES, "permission denied")) is False
    assert is_transport_error(FileNotFoundError(errno.ENOENT, "missing configuration")) is False


def test_is_transport_error_broken_pipe() -> None:
    assert is_transport_error(BrokenPipeError()) is True


def test_is_transport_error_connection_reset() -> None:
    assert is_transport_error(ConnectionResetError()) is True


def test_is_transport_error_connection_refused() -> None:
    assert is_transport_error(ConnectionRefusedError()) is True


def test_is_transport_error_timeout_error() -> None:
    assert is_transport_error(TimeoutError()) is True


def test_is_transport_error_oserror_epipe() -> None:
    assert is_transport_error(OSError(32, "Broken pipe")) is True


def test_is_transport_error_oserror_econnreset() -> None:
    assert is_transport_error(OSError(errno.ECONNRESET, "Connection reset by peer")) is True


# meiva:note
# OSError with a non-transport errno (e.g. ENOENT) is not a transport error.
def test_is_transport_error_oserror_other() -> None:
    assert is_transport_error(OSError(2, "No such file")) is False


def test_is_transport_error_value_error() -> None:
    assert is_transport_error(ValueError("not transport")) is False


def test_is_transport_error_runtime_error() -> None:
    assert is_transport_error(RuntimeError("not transport")) is False


# meiva:note
# MCPError whose __cause__ is BrokenPipeError is a transport error.
def test_is_transport_error_mcp_error_wraps_transport() -> None:
    from mcp import MCPError

    exc = MCPError(-1, "wrapped")
    exc.__cause__ = BrokenPipeError()
    assert is_transport_error(exc) is True


# meiva:note
# MCPError without __cause__ is not a transport error.
def test_is_transport_error_mcp_error_no_cause() -> None:
    from mcp import MCPError

    assert is_transport_error(MCPError(-1, "no cause")) is False


def test_is_transport_error_mcp_error_with_a_cause_that_is_not_transport() -> None:
    from mcp import MCPError

    exc = MCPError(-1, "wrapped")
    exc.__cause__ = ValueError("not transport")
    assert is_transport_error(exc) is False


def test_is_transport_error_mcp_error_follows_a_chain_of_causes() -> None:
    from mcp import MCPError

    inner = MCPError(-1, "inner")
    inner.__cause__ = ConnectionResetError()
    outer = MCPError(-1, "outer")
    outer.__cause__ = inner
    assert is_transport_error(outer) is True


# meiva:note
# The mcp SDK raises `MCPError(CONNECTION_CLOSED)` `from None` when the stdio peer's read loop hits EOF — the __cause__ probe alone missed it, so the daemon never rebuilt the dead session (2026-08-13 #1229). The code must count as a transport error regardless of __cause__.
def test_is_transport_error_mcp_error_connection_closed_no_cause() -> None:
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    assert is_transport_error(MCPError(CONNECTION_CLOSED, "Connection closed")) is True


# meiva:note
# REQUEST_TIMEOUT is the client-side synthesis for a wedged session — same treatment as CONNECTION_CLOSED (parity with the browser-mcp daemon).
def test_is_transport_error_mcp_error_request_timeout_no_cause() -> None:
    from mcp import MCPError
    from mcp.types import REQUEST_TIMEOUT

    assert is_transport_error(MCPError(REQUEST_TIMEOUT, "Timed out")) is True


# meiva:note
# A server-returned JSON-RPC error (invalid params / unknown tool) must not trigger a reconnect — retrying would double-run a side-effectful tool.
def test_is_transport_error_mcp_error_tool_code_no_cause() -> None:
    from mcp import MCPError
    from mcp.types import INVALID_PARAMS

    assert is_transport_error(MCPError(INVALID_PARAMS, "Bad args")) is False


# meiva:note
# The required transport dependency supplies the concrete exception type.
def test_is_transport_error_anyio_broken_resource() -> None:
    from anyio import BrokenResourceError

    assert is_transport_error(BrokenResourceError()) is True


def test_is_transport_error_anyio_closed_resource() -> None:
    from anyio import ClosedResourceError

    assert is_transport_error(ClosedResourceError()) is True
