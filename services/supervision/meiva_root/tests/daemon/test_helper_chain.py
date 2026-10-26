from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.sessions.helper_chain_guard import HELPER_PID_ENV
from services.supervision.meiva_root.daemon import HelperChainWatch
from services.supervision.meiva_root.helper_chain_plan import (
    ChainRound,
    HelperChainConfig,
    evidence_line,
    observe_round,
)

_CHILD = Path(__file__)
_REPO = Path(__file__).resolve().parents[5]
_NOT_AN_ANCESTOR = "2000000000"


def test_config_rejects_a_nonpositive_interval() -> None:
    for interval in (0.0, -1.0, float("inf")):
        with pytest.raises(ValueError):
            HelperChainConfig(interval_s=interval)


def test_config_rejects_an_empty_confirmation() -> None:
    with pytest.raises(ValueError):
        HelperChainConfig(confirm_rounds=0)


def test_a_broken_reading_counts_and_the_confirming_one_retires() -> None:
    first = observe_round(broken_rounds=0, intact=False, confirm_rounds=3)
    second = observe_round(broken_rounds=first.broken_rounds, intact=False, confirm_rounds=3)
    third = observe_round(broken_rounds=second.broken_rounds, intact=False, confirm_rounds=3)

    assert [first, second, third] == [
        ChainRound(broken_rounds=1, retire=False),
        ChainRound(broken_rounds=2, retire=False),
        ChainRound(broken_rounds=3, retire=True),
    ]


def test_an_intact_reading_resets_the_count() -> None:
    assert observe_round(broken_rounds=1, intact=True, confirm_rounds=2) == ChainRound(
        broken_rounds=0, retire=False
    )


def test_the_evidence_line_names_the_root_and_the_expected_helper() -> None:
    line = evidence_line(root_pid=7, marker="4242", broken_rounds=2)

    assert line.startswith("root pid 7 ")
    assert "marker pid 4242" in line
    assert "2 consecutive broken readings" in line


def test_the_evidence_line_reads_an_absent_marker() -> None:
    assert "marker pid absent" in evidence_line(root_pid=7, marker=None, broken_rounds=1)


# meiva:note
# Run the real watch in a child process whose environment carries `marker` (or none) and return
# the retirement lines it recorded. The child is a direct child of this process.
def _watch(marker: str | None, *, run_for_s: float = 0.4, confirm_rounds: int = 2) -> list[str]:
    env = {key: value for key, value in os.environ.items() if key != HELPER_PID_ENV}
    if marker is not None:
        env[HELPER_PID_ENV] = marker
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), str(run_for_s), str(confirm_rounds)],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_process_the_helper_did_not_spawn_is_never_retired() -> None:
    assert _watch(None) == []


def test_a_chain_that_still_reaches_the_helper_is_never_retired() -> None:
    assert _watch(str(os.getpid())) == []


def test_a_chain_that_no_longer_reaches_the_helper_retires_with_the_evidence() -> None:
    retired = _watch(_NOT_AN_ANCESTOR, confirm_rounds=3)

    assert len(retired) == 1
    assert f"marker pid {_NOT_AN_ANCESTOR}" in retired[0]
    assert "3 consecutive broken readings" in retired[0]


def test_a_malformed_marker_is_a_broken_chain() -> None:
    assert len(_watch("not-a-pid")) == 1


# meiva:note
# The child process of the real-watch tests: it runs the real helper-chain watch for a fixed
# time and prints the retirement lines it was handed as one JSON list. The tests launch this file
# with the marker environment they want the real ancestor walk to judge.
async def main(run_for_s: float, confirm_rounds: int) -> None:
    retired: list[str] = []
    stop = asyncio.Event()
    watch = HelperChainWatch(
        retire=retired.append,
        stop=stop,
        config=HelperChainConfig(interval_s=0.01, confirm_rounds=confirm_rounds),
        root_pid=os.getpid(),
    )
    task = asyncio.create_task(watch.run())
    await asyncio.sleep(run_for_s)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    sys.stdout.write(json.dumps(retired) + "\n")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]), int(sys.argv[2])))
