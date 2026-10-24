from __future__ import annotations

import os
from pathlib import Path

from services.agent_runner.page_server import daemon
from services.agent_runner.page_server.tests.page_pty import free_port, running_page_server


def test_the_scan_finds_a_running_page_server_by_port_with_its_home_and_ancestry(
    tmp_path: Path, unit_home: Path
) -> None:
    port = free_port()
    with running_page_server(tmp_path, port, "occupant-token") as server:
        occupants = {occupant.port: occupant for occupant in daemon.page_server_occupants()}

    found = occupants[port]
    assert found.pid == server.pid
    assert found.home == str(unit_home)
    assert found.ancestors[0] == os.getpid()
