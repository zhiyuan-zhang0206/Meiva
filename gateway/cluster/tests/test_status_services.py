# meiva:note
# The Service-processes block lists only gateway-only daemons.
#
# restarter (a per-host daemon) moved to the per-machine roster; labeler +
# memory_indexer stay here as the gateway-only set. Liveness is read from real pidfiles.

import os
import subprocess
import sys
from pathlib import Path

from base.config.tests.configuration import snapshot_process_config
from base.daemon.endpoints import ServiceEndpoints
from gateway.cluster.status import get_services_status


def _endpoints(runtime: Path) -> ServiceEndpoints:
    return ServiceEndpoints.from_boot(snapshot_process_config(), runtime=runtime)


def test_services_status_is_gateway_only(tmp_path: Path) -> None:
    svc = get_services_status(_endpoints(tmp_path))

    names = {item.name for item in svc.items}
    assert names == {"labeler", "memory_indexer"}
    assert "restarter" not in names  # per-host now -> roster, not here


def test_a_live_pidfile_reads_online_with_its_pid(tmp_path: Path) -> None:
    (tmp_path / "labeler.pid").write_text(str(os.getpid()))

    by_name = {item.name: item for item in get_services_status(_endpoints(tmp_path)).items}

    assert (by_name["labeler"].online, by_name["labeler"].pid) == (True, os.getpid())
    assert by_name["labeler"].detail is None
    assert (by_name["memory_indexer"].online, by_name["memory_indexer"].pid) == (False, None)
    assert by_name["memory_indexer"].detail == "pidfile not found"


def test_a_pidfile_of_an_exited_process_reads_dead(tmp_path: Path) -> None:
    # meiva:note
    # The command is the running interpreter with a fixed program: no shell, no user input.
    exited = subprocess.Popen([sys.executable, "--version"])
    exited.wait()
    (tmp_path / "memory_indexer.pid").write_text(str(exited.pid))

    by_name = {item.name: item for item in get_services_status(_endpoints(tmp_path)).items}

    assert (by_name["memory_indexer"].online, by_name["memory_indexer"].pid) == (False, exited.pid)
    assert by_name["memory_indexer"].detail == "pidfile exists but process is dead"
