# meiva:note
# The native LGTM tree under test, built from values.
#
# An `LgtmUnit` is a home under `tmp_path` and the machine identity of the invocation. `render` runs
# the renderer over the checkout's real templates and provisioning tree into real files; the
# database URL, the dashboard render and the settings are values the test passes. `mark_current`
# leaves the version and platform markers of the pinned release, so a converge pass finds every
# backend installed.

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
import yaml
from dotenv import dotenv_values

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.native_process.os_platform import Platform
from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import BACKENDS
from cli.commands.observability import lgtm_native
from cli.commands.observability.inputs import LgtmSettings
from cli.commands.observability.plans.lgtm.dashboard_plan import (
    DashboardOutcome,
    RenderedDashboard,
)
from cli.commands.observability.plans.lgtm.install import platform_tag
from cli.commands.observability.tests.observability_inputs import REPO_ROOT, lgtm_settings
from cli.commands.observability.tests.scripted_http import Reply
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

__all__ = [
    "LOOPBACK_DB",
    "STUB_DASHBOARD",
    "LgtmUnit",
    "Mirror",
    "checkout_pinning",
    "host_on",
    "lgtm_unit",
    "tar_bytes",
    "zip_bytes",
]

# meiva:note
# The dashboard document a render of the metric registries produced, fixed so that these tests
# do not depend on the plugin registry.
STUB_DASHBOARD = RenderedDashboard(
    '{"uid": "meiva-ops-main", "title": "Meiva Ops", "panels": [{"id": 1}]}\n', ()
)

# meiva:note
# The direct database URL of a single box: loopback Postgres on the default cluster port.
LOOPBACK_DB = "postgresql://reader@127.0.0.1:5433/meiva"


@dataclass(frozen=True)
class LgtmUnit:
    home: Path
    machine: ConfiguredMachineIdentity
    producer: Callable[[], EventPipeline]

    @property
    def native(self) -> Path:
        return self.home / "lgtm" / "native"

    # meiva:note
    # Render the native tree with the settings and effects the test names.
    def render(
        self,
        *,
        settings: LgtmSettings | None = None,
        direct_db_url: str = LOOPBACK_DB,
        dashboard: DashboardOutcome = STUB_DASHBOARD,
        storage_dir: Path | None = None,
        producer: Callable[[], EventPipeline] | None = None,
    ) -> None:
        lgtm_native.render_configs(
            REPO_ROOT,
            self.native,
            self.home,
            settings=settings or lgtm_settings(),
            storage_dir=storage_dir or self.native / "data",
            direct_db_url=direct_db_url,
            dashboard=dashboard,
            producer=producer or self.producer,
            machine=self.machine,
        )

    # meiva:note
    # The rendered `runtime.env` as key to value.
    def runtime_env(self) -> dict[str, str | None]:
        return dotenv_values(self.native / "config" / "runtime.env", interpolate=False)

    # meiva:note
    # Leave the markers of the pinned release for `system`/`machine`, as a finished install does.
    def mark_current(self, system: Literal["darwin", "linux"], machine: str) -> str:
        tag = platform_tag(system, machine)
        assert tag is not None
        for name, asset in lgtm_native.native_versions(REPO_ROOT, tag=tag).items():
            assert name in BACKENDS
            self.native.mkdir(parents=True, exist_ok=True)
            (self.native / f"version-{name}").write_text(asset["version"] + "\n", encoding="utf-8")
            (self.native / f"platform-{name}").write_text(tag + "\n", encoding="utf-8")
        return tag


@pytest.fixture
def lgtm_unit(
    tmp_path: Path, command_runtime: CommandRuntime, operator_pipeline: Callable[[], EventPipeline]
) -> LgtmUnit:
    """A home under `tmp_path` with this invocation's machine identity and event producer."""
    return LgtmUnit(tmp_path / "home", command_runtime.machine, operator_pipeline)


# meiva:note
# A host on `platform` that runs real commands: the platform only decides which pinned release
# tag a converge pass installs, so the stub `loki` binary the tests leave behind really runs.
def host_on(platform: Platform) -> Host:
    return Host(run=run_bounded, which=shutil.which, platform=platform, uid=os.getuid(), environ={})


# meiva:note
# A release mirror's archives for one platform, built in memory: a Loki zip whose binary is a shell
# script that answers `-verify-config` with `loki_exit`, a Prometheus tarball and a Grafana release
# tree. `archives()` maps URL path to bytes, `pins(url)` is the `versions.yml` mapping that names
# them at a mirror's base URL.
@dataclass(frozen=True)
class Mirror:
    asset_tag: str
    loki_exit: int = 0

    def loki_script(self) -> bytes:
        return f"#!/bin/sh\necho 'unknown config field' >&2\nexit {self.loki_exit}\n".encode()

    def archives(self) -> dict[str, bytes]:
        return {
            "/loki.zip": zip_bytes(f"loki-{self.asset_tag}", self.loki_script()),
            "/prometheus.tar.gz": tar_bytes(
                {f"prometheus-3.13.2.{self.asset_tag}/prometheus": b"#!/bin/sh\n"}
            ),
            "/grafana.tar.gz": tar_bytes({"grafana-13.2.3/bin/grafana": b"#!/bin/sh\n"}),
        }

    def routes(self, *paths: str) -> dict[str, Reply]:
        archives = self.archives()
        return {path: Reply(body=archives[path]) for path in (paths or tuple(archives))}

    def pins(self, base_url: str) -> dict[str, object]:
        archives = self.archives()

        def asset(path: str) -> dict[str, object]:
            return {
                "url": f"{base_url}{path}",
                "sha256": hashlib.sha256(archives[path]).hexdigest(),
            }

        return {
            "loki": {"version": "3.7.6", "assets": {self.asset_tag: asset("/loki.zip")}},
            "prometheus": {
                "version": "3.13.2",
                "assets": {self.asset_tag: asset("/prometheus.tar.gz")},
            },
            "grafana": {
                "version": "13.2.3",
                "assets": {self.asset_tag: asset("/grafana.tar.gz")},
            },
        }


# meiva:note
# A checkout under `root` carrying the real native templates and provisioning tree and a
# `versions.yml` that names `pins`, so the release the converge pass downloads is the one a test
# serves.
def checkout_pinning(root: Path, pins: dict[str, object]) -> Path:
    for relative in ("deploy/lgtm/native/config", "deploy/lgtm/config/grafana/provisioning"):
        shutil.copytree(REPO_ROOT / relative, root / relative)
    (root / "deploy/lgtm/native/versions.yml").write_text(yaml.safe_dump(pins), encoding="utf-8")
    return root


def zip_bytes(member: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        info = zipfile.ZipInfo(member)
        info.external_attr = 0o755 << 16
        bundle.writestr(info, payload)
    return buffer.getvalue()


def tar_bytes(entries: dict[str, bytes]) -> bytes:
    # meiva:note
    # The archive is a function of `entries` alone, so a second build is byte-identical: the tar is
    # written uncompressed and gzipped with a zero mtime, because a gzip header that records the
    # build time would give the same release another SHA whenever the two builds cross a second.
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as bundle:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(payload))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as archive:
        archive.write(plain.getvalue())
    return compressed.getvalue()
