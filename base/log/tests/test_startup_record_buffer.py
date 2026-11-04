# meiva:note
# base/log — records written before a process's sinks exist (task #6149).
#
# The root supervisor spawns every unit before the participant that installs the file sink has
# started, so a whole generation of `unit … started` lines used to reach a stderr nobody reads.
# `buffer_startup_records()` keeps them and `flush_startup_records()` replays them into the sink with
# the instant, the logger name and the level each was written with.
#
# One run carries its own control: the record logged before the buffer was installed must be absent
# from the same file the buffered one reaches.

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.log import add_file_sink, install_stdlib_intercept, process
from base.paths import logs_dir

_CHILD = Path(__file__).with_name("startup_record_child.py")
_LOGGER = "services.supervision.meiva_root.supervisor"


@pytest.fixture
def restored_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_only_the_records_written_after_the_buffer_was_installed_reach_the_file(
    tmp_path: Path, restored_logging: None
) -> None:
    # meiva:note
    # The floor the root's own `basicConfig(level=INFO)` sets; without it the records never leave
    # the stdlib logger and this test would pass for the wrong reason.
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger(_LOGGER).info("unit exec-host started (pid 1111)")

    process.buffer_startup_records()
    written = datetime.now(UTC)
    logging.getLogger(_LOGGER).info("unit exec-host started (pid 4242)")

    add_file_sink(logs_dir(home=tmp_path) / "meiva-root.log")
    install_stdlib_intercept()
    process.flush_startup_records()

    rows = [
        json.loads(line)
        for line in (logs_dir(home=tmp_path) / "meiva-root.log").read_text().splitlines()
    ]
    records = [row["record"] for row in rows if "started" in str(row["record"].get("message"))]

    assert [record["message"] for record in records] == ["unit exec-host started (pid 4242)"]
    record = records[0]
    assert record["level"]["name"] == "INFO"
    assert record["name"] == _LOGGER
    # meiva:note
    # The instant is the one the record was written at, not the moment the sink appeared: a
    # reader asking "when did this unit start" must not be answered with the end of startup.
    assert abs(float(record["time"]["timestamp"]) - written.timestamp()) < 1.0


# meiva:note
# The composition, not the mechanism: a fresh process that buffers a record and then starts its
# sinks must end up with that record in its own file. This is the half a unit test cannot show --
# `start_process_logging` calling the flush -- and it runs in its own interpreter so the module's
# process-level idempotency guard is exercised exactly once, without patching it.
def test_a_process_replays_what_it_logged_before_starting_its_sinks(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), str(tmp_path)], check=True, timeout=120
    )

    path = logs_dir(home=tmp_path) / "meiva-root.log"
    records = [json.loads(line)["record"] for line in path.read_text().splitlines()]
    assert [record["message"] for record in records] == ["unit svc started (pid 7)"]
