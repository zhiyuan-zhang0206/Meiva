from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness.acp import AcpConnection
from base.lm.harness.acp.tests.eof_process import EofPeer, eof_peer, wait_until


async def _entered_close(connection: AcpConnection, entered: asyncio.Event) -> None:
    entered.set()
    await connection.close()


async def _cleanup(peer: EofPeer, tasks: list[asyncio.Task[None]]) -> None:
    peer.release.touch()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_concurrent_close_waits_for_the_same_child(tmp_path: Path) -> None:
    async with eof_peer(tmp_path, gate_exit=True) as peer:
        first = asyncio.create_task(peer.connection.close())
        tasks = [first]
        try:
            await wait_until(peer.stdin_closed.exists)
            entered = asyncio.Event()
            second = asyncio.create_task(_entered_close(peer.connection, entered))
            tasks.append(second)
            await entered.wait()
            assert not first.done()
            assert not second.done()
            peer.release.touch()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
            assert peer.connection.returncode == 3
            await peer.connection.close()
            assert peer.connection.returncode == 3
        finally:
            await _cleanup(peer, tasks)


async def test_cancelled_close_leaves_teardown_for_the_next_caller(tmp_path: Path) -> None:
    async with eof_peer(tmp_path, gate_exit=True) as peer:
        first = asyncio.create_task(peer.connection.close())
        tasks = [first]
        try:
            await wait_until(peer.stdin_closed.exists)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert peer.connection.returncode is None
            entered = asyncio.Event()
            second = asyncio.create_task(_entered_close(peer.connection, entered))
            tasks.append(second)
            await entered.wait()
            assert not second.done()
            peer.release.touch()
            await asyncio.wait_for(second, 3)
            assert peer.connection.returncode == 3
        finally:
            await _cleanup(peer, tasks)


async def test_cancelling_a_close_waiter_preserves_the_owner(tmp_path: Path) -> None:
    async with eof_peer(tmp_path, gate_exit=True) as peer:
        first = asyncio.create_task(peer.connection.close())
        tasks = [first]
        try:
            await wait_until(peer.stdin_closed.exists)
            entered = asyncio.Event()
            second = asyncio.create_task(_entered_close(peer.connection, entered))
            tasks.append(second)
            await entered.wait()
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert not first.done()
            peer.release.touch()
            await asyncio.wait_for(first, 3)
            assert peer.connection.returncode == 3
        finally:
            await _cleanup(peer, tasks)
