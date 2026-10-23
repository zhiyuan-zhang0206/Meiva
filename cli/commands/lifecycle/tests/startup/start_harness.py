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
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from base.cluster.machine import MachineRoles
from base.deploy.lifecycle.start_serving import RootBirth
from base.host.runtime_interpreter import LoadedRuntimeIdentity
from base.sessions.env_forwarding import managed_service_env
from base.telemetry import EventPipeline
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.root_control.launch_plan import RootLaunchInputs
from cli.commands.lifecycle.start import cmd_start_body, launch_roster
from cli.commands.lifecycle.start.start_config import read_start_config
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.start_runtime import StartRuntime
from services.supervision.roster.service_spec import ServiceSpec

__all__ = ["AdmittedRuntime", "Gateway", "Generation", "StartRun", "gateway_listener", "start_run"]


# meiva:note
# The start runtime whose loaded identity is the one the serving-root double observes.
#
# `StartRuntime` verifies the executing checkout against the process's loaded modules, which a
# temporary git repo is not; the start only needs the identity it admitted to equal the one the
# root reports, so the test says which identity that is.
@dataclass(frozen=True)
class AdmittedRuntime(StartRuntime):
    loaded: LoadedRuntimeIdentity

    def identity(self) -> LoadedRuntimeIdentity:
        return self.loaded

    def validate(self) -> None:
        return None


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
    runtime: AdmittedRuntime
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
            home=self.home,
            service_path=config.service_path,
            remote_data_plane=config.data_plane.is_remote,
        )
        manifest = root_driver.launch_manifest(
            roster,
            self.runtime.code_root,
            roles=roles,
            env=managed_service_env(inputs.service_path),
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
# A committed checkout: what the launch generation binds to.
def _commit(path: Path) -> Path:
    path.mkdir()
    (path / "app.py").write_text("value = 1\n")
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
    runtime = AdmittedRuntime(checkout, checkout, Path(sys.executable), serving_root.runtime)
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


# meiva:note
# A local listener answering 200 on every path: the gateway a pure runner probes.
@dataclass
class Gateway:
    url: str
    requests: list[str]


@contextmanager
def gateway_listener() -> Generator[Gateway]:
    gateway = Gateway("", [])

    class Healthy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            gateway.requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

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
