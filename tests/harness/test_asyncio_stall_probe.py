# meiva:note
# The CI stall probe dumps asyncio chains before faulthandler does.
# This meta-test proves tests/asyncio_stall_probe.py is wired into the session,
# fires inside a real pytest run under -o faulthandler_timeout=N with the chain
# dump landing BEFORE the thread dump, and stays inert without a deadline.

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

import pytest

from tests import asyncio_stall_probe as probe

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SYNTHETIC_HANG = """\
import asyncio


async def test_hangs_forever() -> None:
    await asyncio.Event().wait()
"""


class _StubConfig:
    """The only `pytest.Config` surface `faulthandler_timeout` reads."""

    def __init__(self, value: float | str | None, *, unknown_ini: bool = False) -> None:
        self._value = value
        self._unknown_ini = unknown_ini

    def getini(self, name: str) -> float | str | None:
        if self._unknown_ini:
            raise ValueError(name)
        return self._value


def test_faulthandler_timeout_reads_the_ini_and_defaults_to_zero() -> None:
    assert probe.faulthandler_timeout(cast("pytest.Config", _StubConfig(300))) == 300.0
    assert probe.faulthandler_timeout(cast("pytest.Config", _StubConfig(None))) == 0.0
    disabled_plugin = _StubConfig(None, unknown_ini=True)
    assert probe.faulthandler_timeout(cast("pytest.Config", disabled_plugin)) == 0.0


def test_awaiter_chain_lines_walk_to_the_innermost_await() -> None:
    loop = asyncio.new_event_loop()
    task: asyncio.Task[None] | None = None
    try:

        async def innermost() -> None:
            await asyncio.sleep(30)

        async def outermost() -> None:
            await innermost()

        task = loop.create_task(outermost())
        deadline = time.monotonic() + 10.0
        lines: list[str] = []
        while time.monotonic() < deadline:
            lines = probe.awaiter_chain_lines(task)
            if any(" in sleep" in line for line in lines):
                break
            loop.run_until_complete(asyncio.sleep(0))
        assert any(" in outermost" in line for line in lines), lines
        assert any(" in innermost" in line for line in lines), lines
        assert lines[-1].endswith(" in sleep"), lines
    finally:
        if task is not None:
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()


def test_the_loop_factory_without_a_deadline_is_the_plain_one() -> None:
    with tempfile.TemporaryFile() as sink:
        factory = probe.probing_loop_factory("tests/synthetic.py::test_x", 0.0, sink.fileno())
    assert factory is asyncio.new_event_loop


def test_the_loop_factory_dumps_pending_chains_when_the_deadline_nears() -> None:
    timeout = probe.DUMP_MARGIN_SECONDS + 0.1
    with tempfile.TemporaryFile() as sink:
        factory = probe.probing_loop_factory(
            "tests/synthetic.py::test_hangs", timeout, sink.fileno()
        )
        loop = factory()
        try:
            # meiva:note
            # Bounded wait: the timer is due 0.1s after the loop's creation,
            # so a handful of iterations are enough even on a loaded runner.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and os.fstat(sink.fileno()).st_size == 0:
                loop.run_until_complete(asyncio.sleep(0.02))
        finally:
            loop.close()
        sink.seek(0)
        dumped = sink.read().decode()
    assert "tests/synthetic.py::test_hangs" in dumped, dumped
    assert "--- task " in dumped, dumped
    assert " in sleep" in dumped, dumped


def test_a_loop_closed_before_its_deadline_never_dumps() -> None:
    timeout = probe.DUMP_MARGIN_SECONDS + 0.1
    with tempfile.TemporaryFile() as sink:
        loop = probe.probing_loop_factory(
            "tests/synthetic.py::test_healthy", timeout, sink.fileno()
        )()
        loop.close()
        # meiva:note
        # The probed loop is gone and never ran. A second loop running past the due time must not
        # dump: the timer belonged to the first, which `_pytest.faulthandler` mirrors by giving
        # its own dump the lifetime of the test.
        other = asyncio.new_event_loop()
        try:
            other.run_until_complete(asyncio.sleep(0.2))
        finally:
            other.close()
        sink.seek(0)
        assert sink.read() == b""


def test_the_probe_hook_is_wired_into_this_session(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.hasplugin("tests.asyncio_stall_probe"), (
        "the repo-root conftest.py no longer loads tests/asyncio_stall_probe via pytest_plugins"
    )
    impls = pytestconfig.hook.pytest_asyncio_loop_factories.get_hookimpls()
    wired = [
        impl.function for impl in impls if impl.function is probe.pytest_asyncio_loop_factories
    ]
    assert wired, "the stall-probe loop factory hook is not registered in this session"


def test_the_probe_dumps_before_faulthandler_in_a_real_pytest_run(tmp_path: Path) -> None:
    (tmp_path / "test_probe_e2e.py").write_text(_SYNTHETIC_HANG, encoding="utf-8")
    env = dict(os.environ)
    # A stray addopts (say a local `-n 4 --splits 16`) would re-shape the inner run.
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(  # noqa: S603 — our own interpreter, fixed repo paths
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.asyncio_stall_probe",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "faulthandler_timeout=4",
            # End the inner process at its own deadline instead of letting it
            # hang into this test's timeout: the dump under test fired by then.
            "-o",
            "faulthandler_exit_on_timeout=true",
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    probe_at = combined.find("asyncio stall probe")
    thread_dump_at = combined.find("Timeout (")
    assert probe_at != -1, "the probe never dumped:\n" + combined[-3000:]
    assert thread_dump_at != -1, "faulthandler never dumped:\n" + combined[-3000:]
    assert probe_at < thread_dump_at, (
        "the chain dump must land before the thread dump:\n" + combined[-3000:]
    )
    assert "test_probe_e2e.py::test_hangs_forever" in combined, combined[-3000:]
    assert " in wait" in combined, combined[-3000:]
