# meiva:note
# One whole `meiva start` against real resources, for the tests of the start shell.
#
# What a start decides is asserted on the plans in `../start_plans`. These tests run the shell:
# a home under a short temp directory, a real root-control socket (`scripted_root`) answering as
# the application root, the suite's throwaway Postgres for the schema check and the machine
# registration, a source checkout in a temporary git repo, and a real local listener standing in
# for the gateway a pure runner probes. A cold start's preparation (converge, storage, migrations)
# is not run here: the plan says what it is, and each step has its own tests.

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from base.cluster.machine import MachineRoles
from base.deploy.lifecycle.start_serving import RootBirth
from base.sessions.env_forwarding import managed_service_env
from base.telemetry import EventPipeline
from cli.commands.data_plane.bringup import read_launch_delivery
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.root_control.launch_plan import RootLaunchInputs
from cli.commands.lifecycle.start import cmd_start_body, launch_roster
from cli.commands.lifecycle.start.start_config import read_start_config
from cli.commands.lifecycle.stop.restart import cmd_restart
from cli.commands.lifecycle.tests.runtime_at import runtime_at
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.start_runtime import StartRuntime
from ops.cluster_status import ClusterStatus
from services.supervision.roster.service_spec import ServiceSpec

__all__ = [
    "STATUS_PATH",
    "Gateway",
    "Generation",
    "StartRun",
    "gateway_listener",
    "start_run",
]


# meiva:note
# What a root running the requested roster reports: its launch digest and unit digests.
@dataclass(frozen=True)
class Generation:
    launch_digest: str
    units: dict[str, str]


# meiva:note
# One `meiva start` invocation's inputs, with a short home so a root socket fits.
@dataclass(frozen=True)
class StartRun:
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    producer: Callable[[], EventPipeline]
    runtime: StartRuntime
    children: list[subprocess.Popen[bytes]]
    # meiva:note
    # The home the process environment names. Base owners (serving state, launch-failure notes,
    # the settings-free bootstrap reads) still resolve it themselves; the invocation's own home
    # (`command.home`) is short so a root socket fits under it.
    process_home: Path

    @property
    def home(self) -> Path:
        return self.command.home

    # meiva:note
    # Give the unit the identity `meiva init` would have written to its `.env`.
    #
    # `remote_plane` names a provider-managed data plane in this invocation's configuration,
    # so a launch delivers no per-class login or token to the services it requests.
    def declare(
        self,
        *,
        gateway: bool,
        agent_runner: bool,
        gateway_url: str = "http://127.0.0.1:1",
        remote_plane: bool = False,
    ) -> None:
        config = self.command.config
        config.set_field("machine_name", "start-test")
        config.set_field("gateway_url", gateway_url)
        config.set_field("machine_serve_gateway", True if gateway else None)
        config.set_field("machine_serve_agent_runner", True if agent_runner else None)
        config.set_field("machine_serve_observability_station", None)
        # meiva:note
        # The settings-free bootstrap reads the serve flag from the unit's `.env`: a gateway
        # owns the cluster's configuration, a pure runner fetches it.
        env = self.process_home / ".env"
        env.write_text("MEIVA_MACHINE_SERVE_GATEWAY=true\n" if gateway else "")
        # meiva:note
        # The launch delivery reads the invocation's own home: a gateway's `.env` is there too.
        # A pure runner has no `.env` of its own to read, which is what makes it a runner.
        if gateway:
            (self.home / ".env").write_text("MEIVA_MACHINE_SERVE_GATEWAY=true\n")
        if remote_plane:
            config.set_field("db_url", "postgresql://meiva:pw@db.invalid:5432/meiva")
            config.set_field("redis_url", "rediss://meiva:pw@cache.invalid:6380/0")

    def roster(
        self, roles: MachineRoles, *, only: tuple[str, ...] = (), disabled: tuple[str, ...] = ()
    ) -> tuple[ServiceSpec, ...]:
        return launch_roster(
            roles,
            only_services=only,
            disabled_services=disabled,
            all_services=not only and not disabled,
            machine=self.command.machine,
            host=self.command.host,
        )

    # meiva:note
    # The generation a root that launched `roster` from this checkout would report.
    def generation(self, roles: MachineRoles, roster: tuple[ServiceSpec, ...]) -> Generation:
        config = read_start_config(self.command.config)
        inputs = RootLaunchInputs(
            delivery=read_launch_delivery(
                home=self.home,
                remote_data_plane=config.data_plane.is_remote,
                config=self.command.config,
            ),
            service_path=config.service_path,
        )
        manifest = root_driver.launch_manifest(
            roster,
            self.runtime.code_root,
            roles=roles,
            env=managed_service_env(inputs.service_path, environ=inputs.environment),
            inputs=inputs,
        )
        return Generation(str(manifest["launch_digest"]), root_driver.unit_digests(manifest))

    # meiva:note
    # `cmd_start_body` with one explicit selection form (all services when none is named).
    def start(
        self,
        *,
        only: tuple[str, ...] = (),
        disabled: tuple[str, ...] = (),
        reconcile: bool = True,
        ready_timeout: float = 0.2,
        boot_attempt: bool = False,
    ) -> int:
        return cmd_start_body(
            self.database_factory,
            only_services=only,
            disabled_services=disabled,
            all_services=not only and not disabled,
            reconcile=reconcile,
            ready_timeout=ready_timeout,
            runtime=self.runtime,
            retained_children=self.children,
            producer=self.producer,
            command=self.command,
            boot_attempt=boot_attempt,
        )

    # meiva:note
    # `cmd_restart` over `only` services, with the stop and start budgets a test can afford.
    def restart(
        self,
        *,
        only: tuple[str, ...],
        keep_shell_sessions: bool = False,
        keep_browser: bool = True,
        timeout: float = 15.0,
        start_timeout: float = 0.2,
    ) -> int:
        return cmd_restart(
            only_services=only,
            disabled_services=(),
            all_services=False,
            keep_shell_sessions=keep_shell_sessions,
            keep_browser=keep_browser,
            restart_extras=False,
            force=False,
            timeout=timeout,
            start_timeout=start_timeout,
            runtime=self.runtime,
            retained_children=self.children,
            database_factory=self.database_factory,
            producer=self.producer,
            command=self.command,
        )


# meiva:note
# A committed checkout: what the launch generation binds to.
def _commit(path: Path) -> Path:
    path.mkdir()
    (path / "app.py").write_text("value = 1\n")
    # meiva:note
    # The interpreter every service session launches through: the restart's readiness gate
    # refuses a checkout whose `.venv/bin/python` cannot run.
    python = path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    git = ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
    return path


@pytest.fixture
def start_run(
    redis_url: str,
    short_tmp: Path,
    tmp_path: Path,
    unit_home: Path,
    serving_root: RootBirth,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
    db_url: str,
) -> Iterator[StartRun]:
    """The start's inputs: a short home, a committed checkout and the suite's real database."""
    # meiva:note
    # Requested (redis first) so the command's configuration snapshot names the suite's throwaway
    # Redis and Postgres: `meiva status` dials them, and nothing else on this host.
    del redis_url, db_url
    checkout = _commit(tmp_path / "checkout")
    # meiva:note
    # The identity the start admitted is the one the serving-root double reports as its root's.
    runtime = dataclasses.replace(runtime_at(checkout), loaded=serving_root.runtime)
    run = StartRun(
        # meiva:note
        # No helper: the platform of this run has none (Linux), so root custody is its own birth.
        dataclasses.replace(command_runtime, home=short_tmp, helper=None),
        operator_database,
        operator_pipeline,
        runtime,
        [],
        unit_home,
    )
    yield run


STATUS_PATH = "/api/cluster/status"

# meiva:note
# What a healthy gateway answers `GET /api/cluster/status` with: its own cluster-status snapshot.
HEALTHY_STATUS = ClusterStatus(
    machine_name="gateway-double", serve_gateway=True, serve_agent_runner=False, paused=False
)


# meiva:note
# A local listener answering 200 on every path: the gateway a pure runner probes.
#
# Each request's path and `Authorization` header are kept in order. The status path answers
# `status_body` (a healthy snapshot until a test replaces it), which both the runner's
# liveness probe and the status view `meiva status` prints dial.
@dataclass
class Gateway:
    url: str
    requests: list[str]
    authorizations: list[str | None]
    status_body: bytes


@contextmanager
def gateway_listener() -> Generator[Gateway]:
    gateway = Gateway("", [], [], HEALTHY_STATUS.model_dump_json().encode())

    class Healthy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            gateway.requests.append(self.path)
            gateway.authorizations.append(self.headers.get("Authorization"))
            body = gateway.status_body if self.path == STATUS_PATH else b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Healthy)
    gateway.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
