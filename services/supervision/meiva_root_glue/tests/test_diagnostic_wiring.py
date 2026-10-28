# meiva:note
# The root's diagnostics are `functools.partial` bindings over probes whose parameters are all
# required. A binding that misses one fails only when the probe runs, where the runner folds the
# TypeError into an unobserved sample: nothing turns red. These tests bind every probe the roster
# builds with no further arguments, and read the root's settings by the names the wiring uses.

from __future__ import annotations

import inspect
import json
import os
import socket
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

import psutil

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import get_field, settings
from base.native_process.ownership import OwnedProcess
from base.paths import permissions_helper_socket
from base.telemetry.lgtm_local import backend_urls
from services.desktop.browser.profile import profile_dir
from services.desktop.permissions_helper.launchd_job import LaunchdFacts, helper_job_label
from services.supervision.meiva_root_glue.glue import probe_config_from_settings
from services.supervision.meiva_root_glue.tests.slices import (
    checks_for,
    probe_config,
    system_clock,
)


def _unused_database() -> NoReturn:
    raise AssertionError("this diagnostic must not construct a database")


def test_every_diagnostic_the_roster_builds_has_all_its_required_arguments_bound(
    *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(role="gateway")
    checks = checks_for(
        {"browser", "loki"},
        database=_unused_database,
        machine=machine_identity,
        macos=True,
        clock=system_clock(),
        config=probe_config(pgbouncer_enabled=True, observability_url="http://127.0.0.1:3000"),
    )
    assert {check.name for check in checks} == {
        "brew-pin",
        "venv",
        "permissions-helper",
        "redis-acl",
        "pgbouncer",
        "task-telemetry",
        "observatory-station",
        "browser-reach",
        "lgtm-write-path",
    }
    for check in checks:
        inspect.signature(check.probe).bind()


def test_the_root_reads_each_diagnostic_setting_by_its_registered_name(tmp_path: Path) -> None:
    config = probe_config_from_settings(home=tmp_path)
    assert config.home == tmp_path
    assert config.helper_socket == permissions_helper_socket(
        tmp_path, settings.services.permissions_helper_port
    )
    assert config.redis_url == str(get_field("redis_url"))
    assert config.db_url == str(get_field("db_url"))
    assert config.pgbouncer_enabled is bool(get_field("pgbouncer_enabled"))
    assert config.observability_url == str(get_field("observability_url"))
    assert config.observability_otlp_port == int(get_field("observability_otlp_port"))
    assert config.gateway_health_url == str(get_field("gateway_health_url"))
    assert config.browser_cdp_port == int(get_field("browser_cdp_port"))
    assert config.browser_profile == profile_dir(tmp_path)
    assert config.browser_reach_timeout_s == float(get_field("browser_reach_timeout_s"))
    assert config.browser_reach_probe_interval_s == float(
        get_field("browser_reach_probe_interval_s")
    )
    assert config.loki_url == backend_urls()["loki"]


def test_the_probe_configuration_keeps_credentials_out_of_its_repr() -> None:
    config = probe_config(
        redis_url="redis://runtime:redis-secret@localhost:6380",
        db_url="postgresql://meiva:db-secret@localhost/meiva_main",
        environment={"MEIVA_TOKEN": "env-secret"},
    )
    shown = repr(config)
    assert "redis-secret" not in shown
    assert "db-secret" not in shown
    assert "env-secret" not in shown


# meiva:note
# The helper diagnostic as the root builds it, run for real: it dials the socket its configuration
# names and accepts the answer only from the native parent of the root process it was built with.
# This test process serves the socket and parents the sleeper, so the probe is up only if both the
# socket and the root reached the probe the way the wiring binds them.
def test_the_helper_diagnostic_dials_the_configured_socket_and_watches_the_wired_root(
    *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(role="agent-runner")
    reply = {"id": 0, "ok": True, "result": {"pong": True, "pid": os.getpid()}}
    with tempfile.TemporaryDirectory(prefix="meiva-wiring-", dir="/tmp") as directory:
        sock_path = Path(directory) / "helper.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def serve() -> None:
            conn, _ = server.accept()
            with conn:
                if conn.recv(4096):
                    conn.sendall((json.dumps(reply) + "\n").encode())

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        sleeper = subprocess.Popen(["/bin/sleep", "60"])
        try:
            check = next(
                check
                for check in checks_for(
                    set(),
                    root=OwnedProcess.capture(psutil.Process(sleeper.pid)),
                    database=_unused_database,
                    machine=machine_identity,
                    macos=True,
                    clock=system_clock(),
                    config=probe_config(helper_socket=sock_path),
                )
                if check.name == "permissions-helper"
            )
            result = check.probe()
        finally:
            sleeper.kill()
            sleeper.wait()
            server.close()
            worker.join(timeout=2)
    assert result.alive, result.detail


# meiva:note
# When the helper does not answer, the diagnostic asks launchd for the job of the account and the
# home it was built with: the command it ran names the account's domain and this home's job label.
def test_the_helper_diagnostic_reads_the_launchd_job_of_the_wired_account_and_home(
    tmp_path: Path, *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(role="agent-runner")
    facts = LaunchdFacts(uid=7731, platform="darwin", user_home=tmp_path / "user")
    commands: list[list[str]] = []

    def launchctl(
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        **popen_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout, capture_output, input, popen_kwargs
        commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 113, b"", b"Could not find service")

    check = next(
        check
        for check in checks_for(
            set(),
            launchd=facts,
            run=launchctl,
            database=_unused_database,
            machine=machine_identity,
            macos=True,
            clock=system_clock(),
            config=probe_config(home=tmp_path, helper_socket=tmp_path / "nobody-listens.sock"),
        )
        if check.name == "permissions-helper"
    )
    result = check.probe()
    assert not result.alive
    assert result.detail.startswith("absent")
    assert commands == [["launchctl", "print", f"{facts.domain}/{helper_job_label(tmp_path)}"]]
