from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import services.agent_runner.agent_host.daemon as daemon_mod
from base.config.tests.configuration import snapshot_process_config
from meiva.sdk_surface.installation import Installation


def test_background_loops_are_the_plugin_watch_the_log_rotator_the_memory_guard_and_understanding(
    model_installation: Installation, tmp_path: Path
) -> None:
    loops = daemon_mod.background_loops(
        MagicMock(),
        MagicMock(),
        installation=model_installation,
        config=snapshot_process_config(),
        plugins_root=tmp_path,
    )
    try:
        assert set(loops) == {
            "plugins_watch",
            "stdout_log_rotate",
            "exec_memory_guard",
            "understanding_chunks",
        }
    finally:
        for loop in loops.values():
            loop.close()
