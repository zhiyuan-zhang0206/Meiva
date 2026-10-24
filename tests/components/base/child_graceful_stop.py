# meiva:note
# A product process that installs the graceful-shutdown handler and runs an event loop until it
# is told to stop: meiva-root's `down` must reach the handler and the loop's cleanup must run.
# Usage: child_graceful_stop.py <started-file> <marker-file>

import asyncio
import contextlib
import sys
from pathlib import Path

from base.daemon.shutdown import install_graceful_shutdown


def main(started: Path, marker: Path) -> None:
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
    main(Path(sys.argv[1]), Path(sys.argv[2]))
