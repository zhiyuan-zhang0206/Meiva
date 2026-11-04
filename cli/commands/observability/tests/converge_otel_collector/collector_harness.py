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

import random
import re
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import NoReturn

import pytest
import yaml

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.deploy.release.collector import plan as collector_plan
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.db_authority.api import ensure_telemetry_token, telemetry_token_path
from base.host.net.ports import SYSTEM_URL_OPENER, StreamingUrlOpener
from base.paths import otel_collector_dir
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.observability import otel_collector as oc
from cli.commands.observability.plans.observability_config import (
    DataPlaneInputs,
    ObservabilityConfig,
)
from cli.commands.observability.tests import observability_inputs
from cli.commands.observability.tests.observability_inputs import REPO_ROOT, TELEMETRY_TOKEN
from cli.database.runtime import CommandRuntime, UserSession

__all__ = [
    "GATEWAY",
    "GATEWAY_INGRESS",
    "HYBRID",
    "REPO_ROOT",
    "RUNNER",
    "STATION",
    "CollectorUnit",
    "ConfigMapping",
    "MirrorOpener",
    "collector_unit",
    "config_of",
    "mapping_at",
    "never_dialed",
    "sequence_at",
    "value_at",
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
# A rendered collector config: the template's leaves are strings, numbers, lists and mappings, so a
# section reads as a mapping of `object` values and a test says where it reads and what shape it
# expects — `mapping_at`, `sequence_at`, `value_at` for a leaf — rather than holding an unchecked one.
type ConfigMapping = dict[str, object]


# meiva:note
# The rendered text parsed as YAML, in the one place that accepts what `yaml.safe_load` answers. A
# test that reads the config from the text — and reads the text itself, for a credential that must
# not appear — comes through here instead of binding `Any`.
def config_of(text: str) -> ConfigMapping:
    parsed = object_of(yaml.safe_load, text)
    assert is_object_dict(parsed), "the collector template renders a YAML mapping"
    return parsed


# meiva:note
# The leaf at `path` inside a rendered config. Every step is checked: a string step must name a key
# of a mapping, an integer step must be in range of a sequence. A test that reads through this fails
# with the path it expected when the template stops producing that shape.
def value_at(value: object, *path: str | int) -> object:
    found = value
    for step in path:
        if isinstance(step, int):
            assert is_object_list(found), f"{step!r}: no sequence at {path!r}"
            assert -len(found) <= step < len(found), f"{step!r}: out of range at {path!r}"
            found = found[step]
        else:
            assert is_object_dict(found), f"{step!r}: no mapping at {path!r}"
            assert step in found, f"{step!r}: no such key at {path!r}"
            found = found[step]
    return found


# meiva:note
# The mapping at `path`: `value_at` plus the shape the reader needs.
def mapping_at(value: object, *path: str | int) -> ConfigMapping:
    found = value_at(value, *path)
    assert is_object_dict(found), f"no mapping at {path!r}"
    return found


# meiva:note
# The sequence twin of `mapping_at`.
def sequence_at(value: object, *path: str | int) -> list[object]:
    found = value_at(value, *path)
    assert is_object_list(found), f"no sequence at {path!r}"
    return found


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
    ) -> AbstractContextManager[HTTPResponse]:
        assert isinstance(target, str)
        return SYSTEM_URL_OPENER(f"{self.mirror_url}/{target.rsplit('/', 1)[1]}", timeout)


@dataclass(frozen=True)
class CollectorUnit:
    home: Path
    machine: ConfiguredMachineIdentity
    runtime: CommandRuntime
    open_url: StreamingUrlOpener

    # meiva:note
    # Take back the telemetry token the fixture published: a unit whose gateway never started, or
    # a remote unit that was never issued a capability, holds none.
    def retract_token(self) -> None:
        telemetry_token_path(self.home.resolve()).unlink()

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
    ) -> ConfigMapping:
        return config_of(
            self.text(
                roles,
                host=host,
                observability=observability,
                data_plane=data_plane,
                database_factory=database_factory,
            )
        )

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
        binary = destination / collector_plan.BINARY_NAME
        binary.write_bytes(b"bin")
        (destination / collector_plan.VERSION_MARKER).write_text(
            collector_plan.OTELCOL_CONTRIB_VERSION, encoding="utf-8"
        )
        return binary

    # meiva:note
    # The converge context of this unit, for the step that decides whether to install at all.
    def converge_ctx(self, roles: frozenset[str] | None) -> ConvergeCtx:
        return ConvergeCtx(
            secure_rng=random.SystemRandom(),
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
            clock=self.runtime.clock,
            services=frozenset(),
            terminal=self.runtime.terminal,
        )


@pytest.fixture
def collector_unit(tmp_path: Path, command_runtime: CommandRuntime) -> CollectorUnit:
    """A named machine whose `.meiva` home under `tmp_path` holds a gateway's start intent and
    the telemetry token its start published."""
    observability_inputs.write_cluster_record(tmp_path / ".meiva")
    ensure_telemetry_token((tmp_path / ".meiva").resolve(), TELEMETRY_TOKEN)
    command_runtime.machine.set(name="test-machine")
    return CollectorUnit(
        home=tmp_path / ".meiva",
        machine=command_runtime.machine,
        runtime=command_runtime,
        open_url=SYSTEM_URL_OPENER,
    )
