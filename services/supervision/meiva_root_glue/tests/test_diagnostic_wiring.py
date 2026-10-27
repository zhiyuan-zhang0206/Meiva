# meiva:note
# The root's diagnostics are `functools.partial` bindings over probes whose parameters are all
# required. A binding that misses one fails only when the probe runs, where the runner folds the
# TypeError into an unobserved sample: nothing turns red. These tests bind every probe the roster
# builds with no further arguments, and read the root's settings by the names the wiring uses.

from __future__ import annotations

import inspect
from pathlib import Path
from typing import NoReturn

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import get_field
from base.telemetry.lgtm_local import backend_urls
from services.desktop.browser.profile import profile_dir
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
    assert config.redis_url == str(get_field("redis_url"))
    assert config.db_url == str(get_field("db_url"))
    assert config.pgbouncer_enabled is bool(get_field("pgbouncer_enabled"))
    assert config.observability_url == str(get_field("observability_url"))
    assert config.observability_otlp_port == int(get_field("observability_otlp_port"))
    assert config.gateway_health_url == str(get_field("gateway_health_url"))
    assert config.browser_cdp_port == int(get_field("browser_cdp_port"))
    assert config.browser_profile == profile_dir()
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
