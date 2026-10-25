"""meiva-mcp-daemon healthcheck — a probe returns a bool, never an exception."""

import json
import logging
from pathlib import Path

import pytest

import services.supervision.healthchecks.mcp_daemon as hc


def test_alive_true_when_probe_answers(tmp_path: Path) -> None:
    assert hc.is_alive(tmp_path, probe_fn=lambda _home: True) is True


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("no listener"),
        FileNotFoundError("socket absent"),
        TimeoutError("daemon wedged"),
        json.JSONDecodeError("bad", "", 0),
    ],
    ids=["refused", "no-socket", "timeout", "bad-json"],
)
def test_alive_false_on_expected_probe_failures(exc: Exception, tmp_path: Path) -> None:
    """The transport failures that mean "the daemon is down" stay quiet."""

    def _boom(_home: Path) -> bool:
        raise exc

    assert hc.is_alive(tmp_path, probe_fn=_boom) is False


def test_alive_false_on_any_unexpected_exception(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Any unforeseen probe failure degrades to "dead", with a traceback so
    nothing is swallowed silently."""

    def _boom(_home: Path) -> bool:
        raise RuntimeError("something nobody predicted")

    with caplog.at_level(logging.ERROR, logger="services.supervision.healthchecks.mcp_daemon"):
        assert hc.is_alive(tmp_path, probe_fn=_boom) is False
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)
