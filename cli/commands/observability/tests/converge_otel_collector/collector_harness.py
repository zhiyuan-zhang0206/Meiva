# meiva:note
# The collector sidecar under test, built from values.
#
# A `CollectorUnit` is a home under `tmp_path` whose directory name is the cluster label `.meiva`,
# carrying the start intent a gateway's Postgres receiver reads, and the machine identity of the
# invocation. `render` runs the shell against the shipped template in `deploy/otel-collector` and
# parses the result; `ensure` runs the install pass over real files. The observability and data
# plane values are passed in, never read from settings, and the database is a factory that fails
# the test when it is called, unless the test supplies a real one.

from __future__ import annotations

import re
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
import yaml

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.deploy.release import collector_artifact
from base.host.net.ports import SYSTEM_URL_OPENER, UrlOpener
from base.paths import otel_collector_dir
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.observability import otel_collector as oc
from cli.commands.observability.plans.observability_config import (
    DataPlaneInputs,
    ObservabilityConfig,
)
from cli.commands.observability.tests import observability_inputs
from cli.commands.observability.tests.observability_inputs import REPO_ROOT
from cli.database.runtime import CommandRuntime, UserSession

__all__ = [
    "GATEWAY",
    "GATEWAY_INGRESS",
    "HYBRID",
    "REPO_ROOT",
    "RUNNER",
    "STATION",
    "CollectorUnit",
    "MirrorOpener",
    "collector_unit",
    "never_dialed",
]


GATEWAY = frozenset({"gateway"})
RUNNER = frozenset({"agent-runner"})
STATION = frozenset({"observability-station"})
HYBRID = GATEWAY | RUNNER

# meiva:note
# The authenticated ingress a gateway publishes to its runners.
GATEWAY_INGRESS = "http://10.0.0.10:4318"

# meiva:note
# A `$NAME` still in the rendered text is a template placeholder nobody filled. A literal dollar
# survives on purpose: the network-interface exclusion regexp's end-anchor, written `$$` in the
# template.
_UNFILLED_PLACEHOLDER = re.compile(r"\$[A-Z_]{2,}")


# meiva:note
# A database factory for a render that must not consult the database.
def never_dialed() -> NoReturn:
    raise AssertionError("this render must not open the database")


def never_produced() -> EventPipeline:
    raise AssertionError("the collector step must not emit events")


# meiva:note
# The system opener pointed at a release mirror: whatever URL the pinned download names, the
# request goes to the mirror's path for the same asset file name, over a real socket.
@dataclass(frozen=True)
class MirrorOpener:
    mirror_url: str

    def __call__(
        self, target: str | urllib.request.Request, timeout: float
    ) -> AbstractContextManager[Any]:
        assert isinstance(target, str)
        return SYSTEM_URL_OPENER(f"{self.mirror_url}/{target.rsplit('/', 1)[1]}", timeout)


@dataclass(frozen=True)
class CollectorUnit:
    home: Path
    machine: ConfiguredMachineIdentity
    runtime: CommandRuntime
    open_url: UrlOpener

    # meiva:note
    # The rendered config text for a unit with `roles`, reachable at `host`.
    def text(
        self,
        roles: frozenset[str] | None,
        *,
        host: str = "10.0.0.10",
        observability: ObservabilityConfig | None = None,
        data_plane: DataPlaneInputs | None = None,
        database_factory: DatabaseFactory = never_dialed,
    ) -> str:
        self.machine.set(host=host)
        rendered = oc.generate_config(
            REPO_ROOT,
            self.home,
            roles,
            settings=observability_inputs.collector_settings(
                observability=observability or observability_inputs.observability(),
                data_plane=data_plane or observability_inputs.data_plane(),
            ),
            database_factory=database_factory,
            machine=self.machine,
        )
        assert _UNFILLED_PLACEHOLDER.search(rendered) is None
        return rendered

    # meiva:note
    # The rendered config parsed as YAML.
    def render(
        self,
        roles: frozenset[str] | None,
        *,
        host: str = "10.0.0.10",
        observability: ObservabilityConfig | None = None,
        data_plane: DataPlaneInputs | None = None,
        database_factory: DatabaseFactory = never_dialed,
    ) -> dict[str, Any]:
        parsed: Any = yaml.safe_load(
            self.text(
                roles,
                host=host,
                observability=observability,
                data_plane=data_plane,
                database_factory=database_factory,
            )
        )
        assert isinstance(parsed, dict)
        return cast("dict[str, Any]", parsed)

    # meiva:note
    # Run one install pass over this home. The unit's opener fetches a missing binary: tests that
    # do not download install the pinned binary first, or pass `tag=None`.
    def ensure(
        self,
        roles: frozenset[str] | None,
        *,
        tag: str | None = "linux_amd64",
        platform_label: str = "Linux x86_64",
        host: str = "10.0.0.10",
        observability: ObservabilityConfig | None = None,
        data_plane: DataPlaneInputs | None = None,
    ) -> None:
        self.machine.set(host=host)
        oc.ensure_otel_collector(
            REPO_ROOT,
            self.home,
            roles,
            settings=observability_inputs.collector_settings(
                observability=observability or observability_inputs.observability(),
                data_plane=data_plane or observability_inputs.data_plane(),
            ),
            database_factory=never_dialed,
            machine=self.machine,
            timers=self.runtime.timers,
            open_url=self.open_url,
            tag=tag,
            platform_label=platform_label,
        )

    # meiva:note
    # Put the pinned binary and its version marker where an earlier converge left them.
    def install_pinned_binary(self) -> Path:
        destination = otel_collector_dir(self.home)
        destination.mkdir(parents=True, exist_ok=True)
        binary = destination / collector_artifact.BINARY_NAME
        binary.write_bytes(b"bin")
        (destination / collector_artifact.VERSION_MARKER).write_text(
            collector_artifact.OTELCOL_CONTRIB_VERSION, encoding="utf-8"
        )
        return binary

    # meiva:note
    # The converge context of this unit, for the step that decides whether to install at all.
    def converge_ctx(self, roles: frozenset[str] | None) -> ConvergeCtx:
        return ConvergeCtx(
            repo=REPO_ROOT,
            meiva_home=self.home,
            roles=roles,
            config=self.runtime.config,
            machine=self.machine,
            database_factory=never_dialed,
            producer=never_produced,
            helper=self.runtime.helper,
            host=self.runtime.host,
            session=UserSession(
                user_home=self.home.parent / "user", login_shell="/bin/zsh", interactive=False
            ),
            timers=self.runtime.timers,
            services=frozenset(),
        )


@pytest.fixture
def collector_unit(tmp_path: Path, command_runtime: CommandRuntime) -> CollectorUnit:
    """A named machine whose `.meiva` home under `tmp_path` holds a gateway's start intent."""
    observability_inputs.write_cluster_record(tmp_path / ".meiva")
    command_runtime.machine.set(name="test-machine")
    return CollectorUnit(
        home=tmp_path / ".meiva",
        machine=command_runtime.machine,
        runtime=command_runtime,
        open_url=SYSTEM_URL_OPENER,
    )
