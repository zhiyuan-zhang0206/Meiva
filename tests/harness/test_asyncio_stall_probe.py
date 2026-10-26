# meiva:note
# The CI stall probe dumps asyncio chains before faulthandler does.
# This meta-test proves tests/asyncio_stall_probe.py is wired into the session,
# fires inside a real pytest run under -o faulthandler_timeout=N with the chain
# dump landing BEFORE the thread dump (also when fixture setup has already used part of the
# deadline) and naming the parametrized test, and stays inert without a deadline.

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

import pytest


@pytest.mark.parametrize("variant", ["a"])
async def test_hangs_forever(variant: str) -> None:
    await asyncio.Event().wait()
"""

_SYNTHETIC_SLOW_SETUP = """\
import asyncio
import time

import pytest


@pytest.fixture
def slow_setup() -> None:
    time.sleep(3.0)


async def test_hangs_after_slow_setup(slow_setup: None) -> None:
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


def _running(nodeid: str, *, started: float) -> probe.RunningTest:
    return probe.RunningTest(nodeid=nodeid, started=started)


def test_the_loop_factory_without_a_deadline_is_the_plain_one() -> None:
    running = _running("tests/synthetic.py::test_x", started=time.monotonic())
    with tempfile.TemporaryFile() as sink:
        factory = probe.probing_loop_factory(running, 0.0, sink.fileno(), time.monotonic)
    assert factory is asyncio.new_event_loop


# meiva:note
# What a loop from the factory writes while a task hangs on it, and the seconds it took.
def _dump_of_a_loop_hanging(running: probe.RunningTest, timeout: float) -> tuple[str, float]:
    with tempfile.TemporaryFile() as sink:
        factory = probe.probing_loop_factory(running, timeout, sink.fileno(), time.monotonic)
        loop = factory()
        created = time.monotonic()
        try:
            # meiva:note
            # Bounded wait: a handful of iterations are enough even on a loaded runner when the
            # timer is due within a fraction of a second; a timer that is not due fails the bound.
            deadline = created + 10.0
            while time.monotonic() < deadline and os.fstat(sink.fileno()).st_size == 0:
                loop.run_until_complete(asyncio.sleep(0.02))
        finally:
            loop.close()
        waited = time.monotonic() - created
        sink.seek(0)
        return sink.read().decode(), waited


def test_the_loop_factory_dumps_pending_chains_when_the_deadline_nears() -> None:
    running = _running("tests/synthetic.py::test_hangs[a]", started=time.monotonic())

    dumped, _waited = _dump_of_a_loop_hanging(running, probe.DUMP_MARGIN_SECONDS + 0.1)

    assert "tests/synthetic.py::test_hangs[a]" in dumped, dumped
    assert "--- task " in dumped, dumped
    assert " in sleep" in dumped, dumped


def test_setup_time_already_spent_is_taken_off_the_delay() -> None:
    timeout = probe.DUMP_MARGIN_SECONDS + 30.0
    setup_already_spent = timeout - probe.DUMP_MARGIN_SECONDS + 1.0
    running = _running(
        "tests/synthetic.py::test_slow_setup", started=time.monotonic() - setup_already_spent
    )

    dumped, waited = _dump_of_a_loop_hanging(running, timeout)

    assert "tests/synthetic.py::test_slow_setup" in dumped, dumped
    assert waited < 5.0, waited


def test_the_probe_hook_is_wired_into_this_session(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.hasplugin("tests.asyncio_stall_probe"), (
        "the repo-root conftest.py no longer loads tests/asyncio_stall_probe via pytest_plugins"
    )
    factory_impls = pytestconfig.hook.pytest_asyncio_loop_factories.get_hookimpls()
    assert [
        impl for impl in factory_impls if impl.function is probe.pytest_asyncio_loop_factories
    ], "the stall-probe loop factory hook is not registered in this session"
    protocol_impls = pytestconfig.hook.pytest_runtest_protocol.get_hookimpls()
    assert [impl for impl in protocol_impls if impl.function is probe.pytest_runtest_protocol], (
        "the stall-probe protocol hook (the test's start time and node id) is not registered"
    )


def _inner_run(tmp_path: Path, name: str, source: str) -> str:
    (tmp_path / name).write_text(source, encoding="utf-8")
    env = dict(os.environ)
    # meiva:note
    # A stray addopts (say a local `-n 4 --splits 16`) would re-shape the inner run.
    env.pop("PYTEST_ADDOPTS", None)
    # meiva:note
    # Our own interpreter and fixed repo paths, no shell.
    proc = subprocess.run(  # noqa: S603
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
            # meiva:note
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
    return (proc.stdout or "") + (proc.stderr or "")


def _assert_chain_dump_precedes_thread_dump(combined: str) -> None:
    probe_at = combined.find("asyncio stall probe")
    thread_dump_at = combined.find("Timeout (")
    assert probe_at != -1, "the probe never dumped:\n" + combined[-3000:]
    assert thread_dump_at != -1, "faulthandler never dumped:\n" + combined[-3000:]
    assert probe_at < thread_dump_at, (
        "the chain dump must land before the thread dump:\n" + combined[-3000:]
    )


def test_the_probe_dumps_before_faulthandler_in_a_real_pytest_run(tmp_path: Path) -> None:
    combined = _inner_run(tmp_path, "test_probe_e2e.py", _SYNTHETIC_HANG)

    _assert_chain_dump_precedes_thread_dump(combined)
    assert "test_probe_e2e.py::test_hangs_forever[a]" in combined, combined[-3000:]
    assert " in wait" in combined, combined[-3000:]


def test_the_probe_still_dumps_first_when_fixture_setup_used_most_of_the_margin(
    tmp_path: Path,
) -> None:
    combined = _inner_run(tmp_path, "test_probe_slow_setup.py", _SYNTHETIC_SLOW_SETUP)

    _assert_chain_dump_precedes_thread_dump(combined)
    assert "test_probe_slow_setup.py::test_hangs_after_slow_setup" in combined, combined[-3000:]
