# meiva:note
# The agent host's plugin watcher against a real plugin directory: a plugin is added a moment
# after the watch starts, the watcher asks for the graceful stop, and the loop's cleanup runs.
# Usage: plugin_restart_probe.py <marker-file> <plugins-dir>

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from base.daemon.shutdown import install_graceful_shutdown
from services.agent_runner.agent_host import daemon

install_graceful_shutdown("private-plugin-restart-test")
marker, plugins = Path(sys.argv[1]), Path(sys.argv[2])


async def add_a_plugin_soon() -> None:
    await asyncio.sleep(0.1)
    (plugins / "added").mkdir(parents=True)
    (plugins / "added" / "plugin.py").write_text("PLUGIN = 1\n")


async def run() -> None:
    changing = asyncio.create_task(add_a_plugin_soon())
    try:
        await daemon.watch_plugins_for_restart(plugins, interval_s=0.01)
    finally:
        await changing
        await asyncio.sleep(0.05)
        marker.write_text("clean")


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(run())
