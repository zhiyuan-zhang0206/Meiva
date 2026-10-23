from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psutil

from meiva.mcps._guards import (
    process_facts,
    reap_stale_daemons,
    socket_is_live,
    unlink_own_socket,
)
from meiva.mcps._startup import ProcessFacts
from meiva.mcps.tests.line_streams import leftover_socket
from meiva.mcps.tests.line_streams import short_dir as short_dir


# meiva:note
# A daemon answering the ping protocol = live; a second daemon must not start.
async def test_socket_is_live_true_when_ping_answered(short_dir: Path) -> None:
    release = asyncio.Event()

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(b'{"ok": true}\n')
            await writer.drain()
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    sock = str(short_dir / "live.sock")
    server = await asyncio.start_unix_server(answer, path=sock)
    try:
        # meiva:note
        # `socket_is_live` blocks on a sync socket: run it off the loop so the server can answer.
        assert await asyncio.to_thread(socket_is_live, sock, timeout_seconds=2.0) is True
    finally:
        release.set()
        server.close()
        await server.wait_closed()


# meiva:note
# A socket that accepts but never answers is NOT live: a half-dead occupant must be replaceable, not
# shielded forever by a successful connect.
async def test_socket_is_live_false_when_connect_but_no_reply(short_dir: Path) -> None:
    release = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    sock = str(short_dir / "silent.sock")
    server = await asyncio.start_unix_server(silent, path=sock)
    try:
        assert await asyncio.to_thread(socket_is_live, sock, timeout_seconds=0.3) is False
    finally:
        release.set()
        server.close()
        await server.wait_closed()


async def test_socket_is_live_false_when_the_answer_is_not_ok(short_dir: Path) -> None:
    async def refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b'{"ok": false}\n')
        await writer.drain()
        writer.close()

    sock = str(short_dir / "refuse.sock")
    server = await asyncio.start_unix_server(refuse, path=sock)
    try:
        assert await asyncio.to_thread(socket_is_live, sock, timeout_seconds=2.0) is False
    finally:
        server.close()
        await server.wait_closed()


def test_socket_is_live_false_when_no_listener(short_dir: Path) -> None:
    assert socket_is_live(str(short_dir / "absent.sock"), timeout_seconds=0.3) is False
    assert (
        socket_is_live(str(leftover_socket(short_dir / "dead.sock")), timeout_seconds=0.3) is False
    )


# meiva:note
# The file is unlinked only while it is still the inode this daemon bound.
def test_unlink_own_socket_only_unlinks_the_inode_it_bound(tmp_path: Path) -> None:
    own = tmp_path / "own.sock"
    own.write_text("x")
    unlink_own_socket(str(own), own.stat().st_ino)
    assert not own.exists()

    # meiva:note
    # A later occupant replaced the file (different inode): leave it alone.
    replaced = tmp_path / "replaced.sock"
    replaced.write_text("x")
    unlink_own_socket(str(replaced), replaced.stat().st_ino + 1)
    assert replaced.exists()


def test_unlink_own_socket_of_a_missing_file_is_a_noop(tmp_path: Path) -> None:
    unlink_own_socket(str(tmp_path / "nope.sock"), 123)
    assert not (tmp_path / "nope.sock").exists()


IDLE = Path(__file__).with_name("idle_process.py")
DAEMON_LAUNCH = ["-m", "meiva.mcps._daemon"]


# meiva:note
# A real process that sleeps with the given extra argv (so its command line is exactly what the reaper reads), run from `cwd` with `MEIVA_HOME=home`. It is killed when the block ends.
@contextmanager
def sleeper(argv_tail: list[str], *, cwd: Path, home: Path) -> Generator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(IDLE), *argv_tail],
        cwd=cwd,
        env={**os.environ, "MEIVA_HOME": str(home)},
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def test_process_facts_of_a_daemon_launch_carry_its_unit(short_dir: Path) -> None:
    root, home = (short_dir / "root").resolve(), short_dir / "home"
    root.mkdir()
    home.mkdir()
    with sleeper(DAEMON_LAUNCH, cwd=root, home=home) as proc:
        facts = process_facts(psutil.Process(proc.pid))
    assert facts is not None
    assert facts.pid == proc.pid
    assert facts.cmdline[-2:] == ("-m", "meiva.mcps._daemon")
    assert (facts.cwd, facts.home) == (str(root), str(home))


def test_process_facts_of_any_other_process_leave_its_unit_empty(short_dir: Path) -> None:
    with sleeper(["-m", "something.else"], cwd=short_dir, home=short_dir) as proc:
        facts = process_facts(psutil.Process(proc.pid))
    assert facts is not None
    assert facts == ProcessFacts(proc.pid, facts.cmdline, "", "")


# meiva:note
# One unreadable process skips, never the reap.
def test_process_facts_of_a_process_that_is_gone_is_none(short_dir: Path) -> None:
    with sleeper([], cwd=short_dir, home=short_dir) as proc:
        handle = psutil.Process(proc.pid)
        proc.kill()
        proc.wait()
        assert process_facts(handle) is None


def test_reap_kills_only_the_ghost_daemons_of_this_unit(short_dir: Path) -> None:
    root = (short_dir / "root").resolve()
    elsewhere = (short_dir / "elsewhere").resolve()
    unit_home, other_home = short_dir / "unit", short_dir / "other"
    for directory in (root, elsewhere, unit_home, other_home):
        directory.mkdir()
    wrapper = [
        "python -m meiva.mcps._daemon"
        # meiva:note
        # a `bash -lc` wrapper carries the launch as ONE argv element
    ]

    with (
        sleeper(DAEMON_LAUNCH, cwd=elsewhere, home=unit_home) as by_home,
        sleeper(DAEMON_LAUNCH, cwd=root, home=other_home) as by_root,
        sleeper(DAEMON_LAUNCH, cwd=elsewhere, home=other_home) as other_unit,
        sleeper(wrapper, cwd=root, home=unit_home) as session_wrapper,
        sleeper(["-m", "something.else"], cwd=root, home=unit_home) as bystander,
    ):
        reaped = reap_stale_daemons(root=str(root), home=str(unit_home))

        assert reaped == 2
        assert by_home.wait(timeout=10) == -signal.SIGKILL
        assert by_root.wait(timeout=10) == -signal.SIGKILL
        for survivor in (other_unit, session_wrapper, bystander):
            assert survivor.poll() is None
