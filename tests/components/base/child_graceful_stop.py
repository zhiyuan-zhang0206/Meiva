# meiva:note
# A product process that installs the graceful-shutdown handler and runs an event loop until it
# is told to stop: meiva-root's `down` must reach the handler and the loop's cleanup must run.
# Also provides subprocess entry points for `hard_exit` sink-flush ordering and error reporting.
# Usage:
#   child_graceful_stop.py <started-file> <marker-file>
#   child_graceful_stop.py --hard-exit-order
#   child_graceful_stop.py --hard-exit-broken

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
from pathlib import Path
from typing import cast

from loguru import logger as loguru_logger

from base.host.exit_codes import hard_exit


class _OrderSink:
    def write(self, message: str) -> None:
        del message

    def stop(self) -> None:
        os.write(1, b"loguru\n")


class _BrokenSink:
    def write(self, message: str) -> None:
        del message

    def stop(self) -> None:
        raise RuntimeError("sink close failed")


class _OrderHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record

    def close(self) -> None:
        os.write(1, b"logging\n")
        super().close()


def _run_hard_exit_order() -> None:
    loguru_logger.remove()
    loguru_logger.add(_OrderSink())
    cast("list[object]", vars(logging)["_handlerList"]).clear()
    logging.getLogger().addHandler(_OrderHandler())
    cast("io.TextIOWrapper[io.BufferedWriter]", sys.stdout).__init__(
        io.BufferedWriter(io.FileIO(1, "w", closefd=False), buffer_size=8192),
        encoding="utf-8",
        line_buffering=False,
    )
    cast("io.TextIOWrapper[io.BufferedWriter]", sys.stderr).__init__(
        io.BufferedWriter(io.FileIO(2, "w", closefd=False), buffer_size=8192),
        encoding="utf-8",
        line_buffering=False,
    )
    sys.stdout.write("stdout\n")
    sys.stderr.write("stderr\n")
    hard_exit(7)


def _run_hard_exit_broken() -> None:
    loguru_logger.remove()
    loguru_logger.add(_BrokenSink())
    hard_exit(3)


def main(started: Path, marker: Path) -> None:
    from base.daemon.shutdown import install_graceful_shutdown

    install_graceful_shutdown("private-service-test")

    async def run() -> None:
        try:
            started.write_text("ready")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            marker.write_text("clean")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    if sys.argv[1] == "--hard-exit-order":
        _run_hard_exit_order()
    elif sys.argv[1] == "--hard-exit-broken":
        _run_hard_exit_broken()
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
