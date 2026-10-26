# meiva:note
# The converge step on the observability station: which homes it prepares and what it leaves.
#
# The step runs for real against a home under `tmp_path`: the gating decision (`station_backends`)
# is tested under `plans/`, and these cases prove that a home declaring the station capability
# prepares the full native set with no marker, that the `lgtm-host` marker does the same, and that
# any other home is left untouched. The database is the throwaway Postgres the fixtures provision;
# the pinned markers are current, so nothing is downloaded.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest

from base.agents.context.clients import DatabaseFactory
from base.host.system.tests.fake_commands import DARWIN_ARM64
from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import BACKENDS
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.observability import lgtm_native
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    LgtmUnit,
    host_on,
    lgtm_unit,
)
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    operator_pipeline as operator_pipeline,
)
from cli.commands.observability.tests.lgtm.test_lgtm_native import install_loki_stub
from cli.commands.observability.tests.observability_inputs import REPO_ROOT, lgtm_settings
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

__all__ = ["lgtm_unit"]


def context(
    unit: LgtmUnit,
    runtime: CommandRuntime,
    *,
    roles: frozenset[str],
    database: DatabaseFactory,
    producer: Callable[[], EventPipeline],
) -> ConvergeCtx:
    return ConvergeCtx(
        repo=REPO_ROOT,
        meiva_home=unit.home,
        roles=roles,
        config=runtime.config,
        machine=unit.machine,
        database_factory=database,
        producer=producer,
        helper=runtime.helper,
        host=host_on(DARWIN_ARM64),
        session=UserSession(user_home=unit.home, login_shell="/bin/zsh", interactive=False),
        timers=runtime.timers,
        clock=runtime.clock,
        services=frozenset(BACKENDS),
    )


def test_a_home_declaring_the_station_capability_prepares_the_full_native_set_without_a_marker(
    db_url: str,
    lgtm_unit: LgtmUnit,
    command_runtime: CommandRuntime,
    operator_database: DatabaseFactory,
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)
    ctx = context(
        lgtm_unit,
        command_runtime,
        roles=frozenset({"observability-station"}),
        database=operator_database,
        producer=lgtm_unit.producer,
    )

    lgtm_native.ensure_lgtm_native_step(ctx)

    native = lgtm_unit.native
    for name in ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env"):
        assert (native / "config" / name).is_file()
    assert (native / "grafana/run.sh").is_file()
    assert (native / "data/loki").is_dir()
    assert (native / "data/prom").is_dir()
    rendered_loki = (native / "config/loki.yaml").read_text(encoding="utf-8")
    assert "{{MEIVA_HOME}}" not in rendered_loki
    assert "{{LGTM_STORAGE_DIR}}" not in rendered_loki


def test_the_lgtm_host_marker_makes_a_gateway_home_the_station(
    db_url: str,
    lgtm_unit: LgtmUnit,
    command_runtime: CommandRuntime,
    operator_database: DatabaseFactory,
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)
    (lgtm_unit.home / "lgtm-host").touch()

    lgtm_native.ensure_lgtm_native_step(
        context(
            lgtm_unit,
            command_runtime,
            roles=frozenset({"gateway"}),
            database=operator_database,
            producer=lgtm_unit.producer,
        )
    )

    assert (lgtm_unit.native / "config/loki.yaml").is_file()


def test_an_unmarked_gateway_home_is_left_untouched(
    tmp_path: Path, lgtm_unit: LgtmUnit, command_runtime: CommandRuntime
) -> None:
    def refuse_database() -> NoReturn:
        raise AssertionError("an unmarked home must not reach the database")

    lgtm_native.ensure_lgtm_native_step(
        context(
            lgtm_unit,
            command_runtime,
            roles=frozenset({"gateway", "agent-runner"}),
            database=refuse_database,
            producer=lgtm_unit.producer,
        )
    )

    assert not lgtm_unit.home.exists()


def test_a_station_whose_services_select_no_backend_prepares_nothing(
    lgtm_unit: LgtmUnit, command_runtime: CommandRuntime, operator_database: DatabaseFactory
) -> None:
    ctx = context(
        lgtm_unit,
        command_runtime,
        roles=frozenset({"observability-station"}),
        database=operator_database,
        producer=lgtm_unit.producer,
    )

    lgtm_native.ensure_lgtm_native_step(replace(ctx, services=frozenset({"otel-collector"})))

    assert not lgtm_unit.home.exists()


@pytest.mark.parametrize("configured", [False, True])
def test_the_native_data_volume_is_the_home_by_default_and_the_configured_directory_otherwise(
    db_url: str,
    lgtm_unit: LgtmUnit,
    operator_database: DatabaseFactory,
    tmp_path: Path,
    configured: bool,
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)
    storage = tmp_path / "obs-data" if configured else lgtm_unit.native / "data"

    lgtm_native.ensure_lgtm_native(
        REPO_ROOT,
        lgtm_unit.home,
        services=frozenset(BACKENDS),
        settings=lgtm_settings(),
        storage_dir=storage,
        database_factory=operator_database,
        producer=lgtm_unit.producer,
        machine=lgtm_unit.machine,
        host=host_on(DARWIN_ARM64),
    )

    assert (storage / "loki").is_dir()
    assert (storage / "prom").is_dir()
    assert (lgtm_unit.native / "data").exists() is not configured
