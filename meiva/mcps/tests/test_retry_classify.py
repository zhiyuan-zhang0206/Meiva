from __future__ import annotations

import errno
import sys

import pytest

from meiva.mcps.config import is_transport_error


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


def test_required_dependency_failure_does_not_become_an_empty_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    monkeypatch.setitem(sys.modules, "mcp.types", None)
    with pytest.raises(ImportError, match=r"mcp\.types"):
        is_transport_error(MCPError(CONNECTION_CLOSED, "Connection closed"))


@pytest.mark.parametrize("code", [errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED])
def test_native_transport_errno_is_retryable(code: int) -> None:
    assert is_transport_error(OSError(code, "transport unavailable")) is True


def test_unrelated_io_failure_is_not_a_transport_retry() -> None:
    assert is_transport_error(PermissionError(errno.EACCES, "permission denied")) is False
    assert is_transport_error(FileNotFoundError(errno.ENOENT, "missing configuration")) is False
