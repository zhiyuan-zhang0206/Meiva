# meiva:note
# The gateway child owns the home's root control socket and publishes the serving receipt when
# this suite's gate file appears. A second process reads that gate through the real client, so
# the socket protocol and the receipt are exercised across a process boundary -- the owner
# asserting on its own socket would only prove it to itself -- and a home whose root is gone
# keeps the last server's receipt, which that client must not count as serving.

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from base.deploy.lifecycle import start_serving
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.root_control.tests.status_root import (
    answer_for,
    birth_from_response,
    write_receipt,
)
from base.paths import meiva_home

_WAIT_S = 15.0
_POLL_S = 0.05


class Gate(NamedTuple):
    serving: bool
    recovery: bool


# meiva:note
# Ask a child that owns no root what this home serves and whether it permits recovery.
def _read_gate(gate: Path, report: Path, home: Path) -> Gate:
    report.unlink(missing_ok=True)
    # meiva:note
    # This interpreter, an in-repo module and two paths: no shell, nothing interpolated.
    try:
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "tests.e2e.process_support",
                "reader",
                str(gate),
                "tests.e2e.flow._serving_probe",
                str(report),
                str(home),
            ],
            check=True,
            timeout=60,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("the reader process did not finish within 60s") from exc
    fields = dict(
        line.split("=", 1) for line in report.read_text(encoding="utf-8").split() if "=" in line
    )
    return Gate(serving=fields["serving"] == "True", recovery=fields["recovery"] == "True")


def _await(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_S)
    raise AssertionError(f"{what} did not settle within {_WAIT_S}s")


@pytest.mark.scenario("tests.e2e.fakes.scenario_recording:build_plain")
def test_a_reader_process_reads_the_gate_the_owner_serves(
    gateway_proc: str, tmp_path: Path, short_tmp: Path
) -> None:
    gate = Path(gateway_proc)
    report = tmp_path / "serving.txt"
    home = meiva_home()
    try:
        gate.write_text("ready\n", encoding="utf-8")
        _await(lambda: start_serving.state_path(home).is_file(), "the owner's serving receipt")
        serving = _read_gate(gate, report, home)
        assert serving.serving is True
        assert serving.recovery is True

        gate.unlink()
        _await(lambda: not start_serving.state_path(home).is_file(), "the cleared receipt")
        stopped = _read_gate(gate, report, home)
        assert stopped.serving is False
        # meiva:note
        # The negative of recovery is asserted on its own: a home that stopped serving must not
        # permit a recovery action, and an `is_serving` result cannot stand in for that answer.
        assert stopped.recovery is False

        # meiva:note
        # A marker alone is not serving: a home whose root is gone keeps the receipt of the last
        # server that ran there, and the reader must ask that home's socket instead of counting
        # the file. `short_tmp` keeps that home's socket path inside AF_UNIX's limit.
        crashed = short_tmp / "crashed-home"
        crashed.mkdir()
        write_receipt(crashed, birth_from_response(answer_for(home=crashed)["root"]))
        assert start_serving.state_path(crashed).is_file()
        foreign = _read_gate(gate, report, crashed)
        assert foreign.serving is False
        assert foreign.recovery is False
    finally:
        gate.unlink(missing_ok=True)
