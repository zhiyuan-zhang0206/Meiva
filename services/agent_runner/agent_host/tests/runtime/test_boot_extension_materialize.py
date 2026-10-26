from __future__ import annotations

import inspect
from pathlib import Path

import psycopg
import pytest

from agent.process_boot import init_process_scope, land_cluster_extensions, process_trace_config
from base import db, paths
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.packages.extensions import registry as reg
from base.telemetry.otlp.trace_config import TraceConfig

_SKILL_MD = """---
name: {name}
description: a test skill, use when exercising boot materialization
---

# {name}

Instructions.
"""


def _write_skill(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")
    return root


def test_boot_lands_a_skill_this_machine_never_installed(
    tmp_path: Path,
    unit_home: Path,
    db_conn: psycopg.Connection,
    database: Database,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _ = db_conn
    src = _write_skill(tmp_path / "src" / "boot-demo", "boot-demo")
    reg.register_tree(
        db.pool(gate=database_gate),
        root=src,
        name="boot-demo",
        kind="skill",
        source="local:some-other-machine",
    )
    assert not (unit_home / "skills" / "boot-demo").exists(), (
        "this machine must start without it, or the test proves nothing"
    )

    land_cluster_extensions(database)

    assert (paths.skills_dir(paths.meiva_home()) / "boot-demo" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == (src / "SKILL.md").read_text(encoding="utf-8")


def test_an_unreadable_registry_does_not_stop_the_boot(unit_home: Path, database: Database) -> None:
    import dataclasses

    unreachable = Database(
        dataclasses.replace(database._config, db_url="postgresql://127.0.0.1:1/meiva"),
        gate=database._gate,
    )
    land_cluster_extensions(unreachable)

    assert not (unit_home / "skills").exists()


def test_the_hosted_daemon_lands_them_once_per_process() -> None:
    from services.agent_runner.agent_host.daemon import run

    src = inspect.getsource(run)
    assert src.index("init_process_scope(") < src.index("land_cluster_extensions(db)")
    assert "load_process_extensions" not in src, "the plugin load is `load_extensions` alone"
    assert src.count("land_cluster_extensions(db)") == 1, (
        "once per process — a second call would re-hash every skill tree for no gain"
    )


def test_process_scope_passes_its_machine_and_home_without_reading_identity(
    unit_home: Path,
) -> None:
    from base.telemetry import tracing

    (unit_home / ".env").write_bytes(b"\xff")
    config = ConfigBoot()
    machine = ConfiguredMachineIdentity(config)
    trace = TraceConfig(
        enabled=True,
        strip_content=True,
        endpoint="http://127.0.0.1:4318",
        retention_days=14,
        max_dir_mb=2048,
        disk_watermark=0.0,
        endpoint_explicit=True,
    )
    tracing.reset_tracing_state()
    init_process_scope(machine=machine, home=unit_home, trace=trace)
    assert not config.prepared
    assert (unit_home / "traces").is_dir()
    with pytest.raises(UnicodeDecodeError):
        machine.name()


@pytest.mark.parametrize(
    ("launch_environment", "explicit"),
    [({}, False), ({"MEIVA_TELEMETRY_OTLP_ENDPOINT": "http://collector.invalid:4318"}, True)],
)
def test_the_process_trace_config_records_whether_the_launch_named_the_endpoint(
    launch_environment: dict[str, str], explicit: bool
) -> None:
    config = snapshot_process_config()
    config.environment = launch_environment

    assert process_trace_config(config).endpoint_explicit is explicit
