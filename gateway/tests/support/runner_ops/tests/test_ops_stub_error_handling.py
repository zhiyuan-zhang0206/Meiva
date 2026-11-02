# meiva:note
# What the ops stub's listener does when the caller leaves before its answer is written.
#
# The stub stands in for another machine's `POST /ops`. A dispatch that times out, a request a test
# cancels or a probe it abandons closes the socket first, so the answer is written to a connection
# nobody reads and the write raises in a request thread. Those threads are daemons and
# `server_close()` never joins them, so the traceback used to surface in whatever `sys.stderr` was
# at that moment -- a later test's captured output, which made an exact-stderr assertion fail a
# worker away from its cause (`cli/parsers/tests/test_parser_contract.py`, PR #8227's run).
#
# These cases drive `handle_error` directly: no socket traffic, no waiting, and both outcomes are
# the subject -- the caller's failure is silent, a failure of the handler itself is not.

from __future__ import annotations

import socket

import pytest

from gateway.tests.support.runner_ops import ops_stub


def _request_that_ended_in(server: ops_stub.OpsServer, failure: BaseException) -> None:
    # meiva:note
    # What a request thread does when its write fails: `handle_error` runs while that exception is
    # the one being handled.
    with socket.socket() as request:
        try:
            raise failure
        except type(failure):
            server.handle_error(request, ("127.0.0.1", 0))


def _listener() -> ops_stub.OpsServer:
    # meiva:note
    # The listener `runner_ops` serves on, without the registry row that needs a database.
    return ops_stub.listener(ops_stub.RunnerOps(name="probe", url=""), {})


def test_a_caller_that_left_before_its_answer_writes_nothing_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _listener() as server:
        _request_that_ended_in(server, BrokenPipeError(32, "Broken pipe"))

    assert capsys.readouterr().err == ""


def test_a_caller_that_reset_the_connection_writes_nothing_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _listener() as server:
        _request_that_ended_in(server, ConnectionResetError(104, "Connection reset by peer"))

    assert capsys.readouterr().err == ""


def test_a_failure_of_the_handler_itself_still_reaches_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _listener() as server:
        _request_that_ended_in(server, ValueError("a bug in this handler"))

    captured = capsys.readouterr().err
    assert "a bug in this handler" in captured
    assert "ValueError" in captured
