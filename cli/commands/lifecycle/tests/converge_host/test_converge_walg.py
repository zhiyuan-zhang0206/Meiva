# meiva:note
# The WAL-G converge step: inert while off, strict before Postgres starts while on.
#
# Installing the pinned binary downloads it, so the step's plan is asserted whole while the steps
# that touch the home (the configuration and its pinned key, the state directory) run for real.

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.paths.tests.homes import unit_home as unit_home
from cli.commands.converge import walg as converge_walg_module
from cli.commands.converge.os_jobs import ensure_walg_job
from cli.commands.converge.plans.walg_plan import (
    InstallWalgBinary,
    LoadWalgConfig,
    PrepareWalgStateDir,
    RefuseWalg,
    plan_walg_converge,
)
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host
from services.backup.walg import config as walg_config
from services.backup.walg.tests.support import KEY_HEX, valid_config

# meiva:note
# --- the plan (pure) ----------------------------------------------------------------


def test_while_off_there_is_nothing_to_do_whatever_the_data_plane_is() -> None:
    assert plan_walg_converge(configured=False, remote_plane=False) == ()
    assert plan_walg_converge(configured=False, remote_plane=True) == ()


def test_while_on_the_binary_comes_first_then_the_configuration_and_the_state_directory() -> None:
    plan = plan_walg_converge(configured=True, remote_plane=False)

    assert plan == (InstallWalgBinary(), LoadWalgConfig(), PrepareWalgStateDir())


def test_a_remote_managed_postgres_cannot_archive() -> None:
    plan = plan_walg_converge(configured=True, remote_plane=True)

    assert plan == (
        RefuseWalg(
            "MEIVA_WALG_CONFIG_FILE is set but this cluster's Postgres is remote-managed: "
            "WAL archiving needs the locally launched Postgres (unset the key)"
        ),
    )


def test_the_step_sits_between_the_postgres_runtime_and_the_pooler_on_gateways() -> None:
    names = [step.name for step in converge_host.CONVERGE_STEPS]
    step = converge_host.CONVERGE_STEPS[names.index("WAL-G archiving")]

    assert step.apply is converge_walg_module.converge_walg
    assert step.roles == frozenset({"gateway"})
    assert step.host_global is False
    assert names.index("PostgreSQL 17 + pgvector runtime") < names.index("WAL-G archiving")
    assert names.index("WAL-G archiving") < names.index(
        "one DB URL + pgbouncer binary (when enabled)"
    )


# meiva:note
# --- the step against a real home ---------------------------------------------------


# meiva:note
# A gateway context on a fresh lazy configuration owner, so a test can see what forces it.
#
# `home` is the process's home (`unit_home`), where the key pin lives; when WAL-G is on the
# owner names a valid configuration and key file under it.
def _walg_ctx(
    ctx: ConvergeCtx, home: Path, *, enabled: bool = True, remote: bool = False
) -> ConvergeCtx:
    store = home / "secrets" / "walg"
    store.mkdir(parents=True, exist_ok=True)
    key_file = store / "libsodium.key"
    key_file.write_text(KEY_HEX + "\n")
    key_file.chmod(0o600)
    config_file = store / "walg.json"
    config_file.write_text(json.dumps(valid_config(key_file)))
    config_file.chmod(0o600)
    config = ConfigBoot()
    config.set_field("walg_config_file", config_file if enabled else None)
    db_host = "192.0.2.10" if remote else "127.0.0.1"
    config.set_field("db_url", f"postgresql://test:test@{db_host}:5433/meiva")
    config.set_field("redis_url", "redis://127.0.0.1:6380/0")
    return replace(
        ctx,
        repo=home,
        meiva_home=home,
        roles=frozenset({"gateway"}),
        config=config,
        machine=ConfiguredMachineIdentity(config),
    )


def _config_file(on: ConvergeCtx) -> Path:
    path = on.config.view.walg.walg_config_file
    assert path is not None
    return path


def test_off_installs_and_writes_nothing(ctx: ConvergeCtx, unit_home: Path) -> None:
    off = _walg_ctx(ctx, unit_home, enabled=False, remote=True)

    # meiva:note
    # reaching the installer would download wal-g
    converge_walg_module.converge_walg(off)

    assert not (unit_home / "backups").exists()
    assert not off.config.is_full()


def test_on_validates_pins_the_key_and_makes_the_state_directory(
    ctx: ConvergeCtx, unit_home: Path
) -> None:
    on = _walg_ctx(ctx, unit_home)

    # meiva:note
    # The plan's installer step is left out: running it would download the pinned wal-g.
    converge_walg_module.execute_walg_plan(
        (LoadWalgConfig(), PrepareWalgStateDir()), on, walg_config_file=_config_file(on)
    )

    assert (
        walg_config.pinned_key_id(unit_home)
        == walg_config.read_config(_config_file(on)).key_fingerprint
    )
    state = unit_home / "backups" / "walg"
    assert state.is_dir() and state.stat().st_mode & 0o777 == 0o700


def test_a_bad_configuration_fails_converge_not_postgres(ctx: ConvergeCtx, unit_home: Path) -> None:
    on = _walg_ctx(ctx, unit_home)
    key_file = unit_home / "secrets" / "walg" / "libsodium.key"
    _config_file(on).write_text(
        json.dumps(valid_config(key_file, WALG_PREVENT_WAL_OVERWRITE="false"))
    )

    with pytest.raises(walg_config.WalgConfigError, match="WALG_PREVENT_WAL_OVERWRITE"):
        converge_walg_module.execute_walg_plan(
            (LoadWalgConfig(),), on, walg_config_file=_config_file(on)
        )

    assert walg_config.pinned_key_id(unit_home) is None, "an unusable configuration pins nothing"


def test_a_different_key_than_the_pinned_one_fails_converge(
    ctx: ConvergeCtx, unit_home: Path
) -> None:
    on = _walg_ctx(ctx, unit_home)
    converge_walg_module.execute_walg_plan(
        (LoadWalgConfig(),), on, walg_config_file=_config_file(on)
    )
    (unit_home / "secrets" / "walg" / "libsodium.key").write_text("cd" * 32 + "\n")

    with pytest.raises(walg_config.WalgConfigError, match="is not the key this home pinned"):
        converge_walg_module.execute_walg_plan(
            (LoadWalgConfig(),), on, walg_config_file=_config_file(on)
        )


def test_a_remote_managed_data_plane_cannot_archive(ctx: ConvergeCtx, unit_home: Path) -> None:
    with pytest.raises(RuntimeError, match="remote-managed"):
        converge_walg_module.converge_walg(_walg_ctx(ctx, unit_home, remote=True))

    assert not (unit_home / "backups").exists()


# meiva:note
# --- the daily tick job follows the key ---------------------------------------------


def test_the_job_step_runs_on_gateways_after_the_other_scheduled_jobs() -> None:
    names = [step.name for step in converge_host.CONVERGE_STEPS]
    step = converge_host.CONVERGE_STEPS[names.index("WAL-G backup job")]

    assert step.apply is ensure_walg_job
    assert step.roles == frozenset({"gateway"})
    assert names.index("PR flow sampler job") < names.index("WAL-G backup job")
