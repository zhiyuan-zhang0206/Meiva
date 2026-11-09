# meiva:note
# base/log -- no sink renders a traceback frame's local variables (task #6291).
#
# `add_sink` already opens every sink of ours with `diagnose=False`, and #8536 stopped one
# hand-picked value from being rendered. Neither closes the class: loguru's own default stderr
# handler exists from `import loguru`, and the processes that never call
# `prepare_process_logging()` keep it when they open their own sink -- an exec host, the MCP daemon,
# the pty session server, the exec child.
#
# Each mode runs in its own interpreter and the assertion reads the child's stderr, not a flag: a
# guard that removed every handler would also make the flag read clean, and it would have nothing to
# do with the leak this test exists for.

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from loguru import logger

from base.log.diagnose import diagnosing_handler_ids, disable_frame_variable_rendering
from base.log.sinks import add_sink, guard_default_logging

_CHILD = Path(__file__).with_name("diagnose_guard_child.py")
_SENTINEL = "SENTINEL-PASSWORD-123"


# meiva:note
# loguru colorizes a record by wrapping each token in an escape sequence, so a literal spanning two
# tokens ("RuntimeError: boom") is split by a reset and never matches -- the assertion would then
# test the colorizer rather than the guard. Strip the escapes and assert on the tokens that matter:
# the failure survives, the frame's local value does not.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _discard(_message: object) -> None:
    return None


# meiva:note
# The child is this repository's own test helper, run by path with this interpreter; there is no
# shell, no PATH lookup and no untrusted argument.
def _run(mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), mode], capture_output=True, text=True, check=True, timeout=120
    )


# meiva:note
# The positive control, and the reason the two modes after it are evidence: a probe that cannot see
# the sentinel would pass them just as happily.
def test_a_sink_added_directly_still_renders_frame_locals() -> None:
    done = _run("raw-add")

    assert done.stdout.strip() != "diagnosing handlers: []"
    assert _SENTINEL in _plain(done.stderr)


def test_a_process_with_its_own_sink_renders_no_frame_locals() -> None:
    done = _run("own-sink")
    output = _plain(done.stderr)

    assert done.stdout.strip() == "diagnosing handlers: []"
    assert _SENTINEL not in output
    assert "boom" in output


def test_add_sink_disables_a_handler_registered_before_it() -> None:
    inherited = logger.add(_discard)
    try:
        assert inherited in diagnosing_handler_ids()
        opened = add_sink(_discard, level="INFO")
        try:
            assert inherited not in diagnosing_handler_ids()
        finally:
            logger.remove(opened)
    finally:
        logger.remove(inherited)


def test_the_guard_names_what_it_changed_and_is_idempotent() -> None:
    handler_id = logger.add(_discard)
    try:
        assert handler_id in disable_frame_variable_rendering()
        assert handler_id not in diagnosing_handler_ids()
        assert handler_id not in disable_frame_variable_rendering()
    finally:
        logger.remove(handler_id)


# meiva:note
# Verify that `guard_default_logging` closes frame-variable rendering and installs redaction on
# loguru's default stderr handler 0 in a fresh interpreter without `prepare_process_logging` or
# `add_sink` (task #6594).
def test_guard_default_logging_protects_default_stderr_handler_in_bare_subprocess() -> None:
    done = _run("default-guard")
    output = _plain(done.stderr)
    motive = (
        "guard_default_logging must disable _diagnose and install redaction on Loguru's default "
        "stderr handler 0 in bare subprocesses that never call prepare_process_logging or add_sink: "
        "on 2026-11-09 (task #6594) an SDK child process hit a gateway outage and handler 0 "
        "rendered stack-frame locals (Authorization Bearer token) into stderr while keeping "
        f"exception type and call site lines; see base/log/sinks.py and base/log/diagnose.py. output={output!r}"
    )

    assert guard_default_logging is not None, motive
    assert done.stdout.strip() == "diagnosing handlers: []", motive
    assert _SENTINEL not in done.stdout and _SENTINEL not in done.stderr, motive
    assert "└ " not in output, motive
    assert [
        t for t in ("db_password", "boom", "RuntimeError", "MEIVA_API_TOKEN=***") if t not in output
    ] == [], motive
