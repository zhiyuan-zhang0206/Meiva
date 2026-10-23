from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import services.agent_runner.agent_host.daemon as daemon_mod
from base.config import ConfigBoot
from meiva.sdk_surface.installation import Installation


def test_background_loops_are_the_plugin_watch_the_log_rotator_the_memory_guard_and_understanding(
    model_installation: Installation,
) -> None:
    with patch.dict(os.environ):
        loops = daemon_mod.background_loops(
            MagicMock(),
            MagicMock(),
            installation=model_installation,
            config=ConfigBoot(),
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
