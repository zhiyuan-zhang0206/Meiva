from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from base.agents.context.clients import ClientSet
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.lifecycle import start_serving
from base.host.exit_codes import hard_exit
from base.host.system.hostport import Host
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline, build_pipeline

__all__ = ["GatewayProcess"]


# meiva:note
# The configuration, admission and writer owned by one Gateway entry.
class GatewayProcess:
    def __init__(
        self,
        *,
        home: Path,
        config: ConfigBoot,
        image: LoadedCommit,
        machine: MachineIdentityReader,
        host: Host,
        observe: Callable[[Path], start_serving.RootBirth] = start_serving.observe_root,
    ) -> None:
        self.home = home
        self.config = config
        self.machine = machine
        self.host = host
        # meiva:note
        # How this process sees the running root: `observe_root` itself unless the caller names
        # another. Every reader of the serving gate on a request path takes it from here, so one
        # process sees one observation.
        self.observe = observe
        self.image = image
        self.version = CodeVersion(image)
        self.gate = ProcessDbGate(
            version=self.version.get, process="gateway", end_process=hard_exit
        )
        self.clients = ClientSet(
            database=self.database,
            pipeline_factory=lambda: build_pipeline(
                otlp_enabled=lambda: self.config.view.observability.telemetry_otlp_enabled,
                otlp_endpoint=lambda: self.config.view.observability.telemetry_otlp_endpoint,
                database=self.database,
                machine=self.machine,
                home=self.home.resolve(),
            ),
        )

    # meiva:note
    # Read the current root config, retaining this entry's original gate.
    def database(self) -> Database:
        return Database(
            db_config_from_boot(self.config),
            gate=self.gate,
            direct_url=ClusterDirectUrl(self.machine.host, home=self.home.resolve()),
        )

    # meiva:note
    # Open the writer on first use; logging and ASGI reuse the same object.
    def event_pipeline(self) -> EventPipeline:
        return self.clients.event_pipeline()

    # meiva:note
    # Close constructed clients even when startup or serving raises.
    @contextmanager
    def lifetime(self) -> Generator[GatewayProcess]:
        primary: BaseException | None = None
        try:
            yield self
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                self.clients.close()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"Gateway writer cleanup also failed: {cleanup!r}")
