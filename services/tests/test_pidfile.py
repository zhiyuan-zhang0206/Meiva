from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from base.config import ConfigBoot
from base.host.proc import process_cmdline
from services.agent_runner.agent_ops import boot as ops_boot
from services.agent_runner.agent_ops import daemon as agent_ops_daemon
from services.agent_runner.page_server import daemon as page_server_daemon
from services.backup.scheduler import daemon as backup_scheduler_daemon
from services.derived.labeler import daemon as labeler_daemon
from services.derived.memory_indexer import daemon as memory_indexer_daemon
from services.entrypoints.im_bridge import daemon as im_bridge_daemon
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.upkeep.events_maintenance import daemon as events_maintenance_daemon
from services.wake.delivery_watchdog import daemon as delivery_watchdog_daemon
from services.wake.heartbeat import daemon as heartbeat_daemon


def _guard(daemon: ModuleType, config: ConfigBoot, runtime: Path) -> tuple[Path, bool]:
    module_name = daemon.__name__
    if module_name == "services.agent_runner.page_server.daemon":
        resources = daemon.resources
        return resources.pidfile(config, runtime=runtime), resources.is_running(
            config, runtime=runtime
        )
    if module_name == "services.agent_runner.agent_ops.daemon":
        return ops_boot.pidfile(config, runtime=runtime), ops_boot.is_running(
            config, runtime=runtime
        )
    return daemon._pidfile(config, runtime=runtime), daemon._is_running(config, runtime=runtime)


@pytest.mark.parametrize(
    "daemon",
    [
        labeler_daemon,
        heartbeat_daemon,
        backup_scheduler_daemon,
        page_server_daemon,
        im_bridge_daemon,
        memory_indexer_daemon,
        delivery_watchdog_daemon,
        events_maintenance_daemon,
        agent_ops_daemon,
    ],
    ids=lambda module: module.__name__.split(".")[-2],
)
def test_daemon_guard_checks_only_current_pidfile(
    daemon: ModuleType,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    module_name = daemon.__name__
    runtime = tmp_path / "run"
    runtime.mkdir(parents=True, exist_ok=True)
    # meiva:note
    # Spawn a child using the fixed interpreter and module argv so pidfile guard tests see a live PID.
    holder = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "services.tests.test_pidfile", module_name],
        stdin=subprocess.PIPE,
    )
    try:
        (runtime / "unrelated.pid").write_text(f"{holder.pid}\n", encoding="utf-8")
        pidfile, running = _guard(daemon, process_config, runtime)
        assert running is False

        pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
        _, running = _guard(daemon, process_config, runtime)
        assert running is False

        pidfile.write_text(f"{holder.pid}\n", encoding="utf-8")
        _, running = _guard(daemon, process_config, runtime)
        assert running is True
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.kill()
        holder.wait(timeout=5)


# meiva:note
# The pidfile records the pid the claiming entry hands in, not one this module reads: the
# number below belongs to no live process, so nothing but the written contents can match it.
def test_acquire_writes_the_pid_it_is_given(tmp_path: Path) -> None:
    pidfile = tmp_path / "svc.pid"
    assert acquire_pidfile(pidfile, "svc.daemon", pid=4242) is True
    assert pidfile.read_text().strip() == "4242"


def test_acquire_refuses_live_instance(tmp_path: Path) -> None:
    pidfile = tmp_path / "svc.pid"
    cmdline = process_cmdline(os.getpid())
    assert cmdline
    identity = " ".join(cmdline)
    assert acquire_pidfile(pidfile, identity, pid=os.getpid()) is True
    assert acquire_pidfile(pidfile, identity, pid=os.getpid()) is False
    assert pidfile.read_text().strip() == str(os.getpid())


def test_acquire_reclaims_stale_dead_pid(tmp_path: Path) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text("99999999\n")
    assert pidfile_holds_daemon(pidfile, "svc.daemon") is False
    assert acquire_pidfile(pidfile, "svc.daemon", pid=os.getpid()) is True
    assert pidfile.read_text().strip() == str(os.getpid())


def test_acquire_reclaims_recycled_pid(tmp_path: Path) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text(f"{os.getpid()}\n")
    assert pidfile_holds_daemon(pidfile, "svc.daemon") is False
    assert acquire_pidfile(pidfile, "svc.daemon", pid=os.getpid()) is True


def test_pidfile_holds_daemon_false_when_argv_mismatch(tmp_path: Path) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text(f"{os.getpid()}\n")
    cmdline = process_cmdline(os.getpid())
    assert cmdline
    identity = " ".join(cmdline)
    assert "services.svc.daemon" not in identity
    assert pidfile_holds_daemon(pidfile, "services.svc.daemon") is False
    assert pidfile_holds_daemon(pidfile, identity) is True


def test_remove_pidfile_idempotent(tmp_path: Path) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text("1\n")
    remove_pidfile(pidfile)
    assert not pidfile.exists()
    remove_pidfile(pidfile)


if __name__ == "__main__":
    sys.stdin.read()
