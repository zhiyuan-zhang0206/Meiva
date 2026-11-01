from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest

from base import cluster
from base.host.env import port_lease
from base.paths.tests.homes import point_home
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.start_identity import IdentityInput, gateway_values, read_intent
from cli.startup.tests.birth_facts import birth_facts
from services.backup.artifact import passphrase

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ConfiguredSingleBox:
    home: Path
    record: cluster.ClusterRecord
    values: dict[str, str]
    plane: DataPlaneConfig


def write_configured_intent(
    home: Path, record: cluster.ClusterRecord, values: dict[str, str]
) -> None:
    body = {
        "version": 1,
        "home": str(home),
        "checkout": str(REPO_ROOT),
        "roles": ["agent-runner", "gateway"],
        "config_digest": None,
        "phase": "configured",
        "record": asdict(record),
        "env": values,
    }
    path = home / "start-intent.json"
    path.write_text(json.dumps(body, sort_keys=True) + "\n")
    path.chmod(0o600)


def configure_single_box_home(
    home: Path,
    distinct_free_ports: Callable[[int], list[int]],
    unit_plane: DataPlaneConfig,
) -> ConfiguredSingleBox:
    home.mkdir(mode=0o700, exist_ok=True)
    # meiva:note
    # Allocate the entire roster at once. The lease hands out a number it never repeats
    # while this session lives, so the slots cannot collide with each other, with another
    # holder on the host, or with a later fixture that asks for one more port.
    ports = cluster.new_home_ports()
    for name, port in zip(
        cluster.FIXED_PORTS, distinct_free_ports(len(cluster.FIXED_PORTS)), strict=True
    ):
        ports[name] = port
    record = cluster.ClusterRecord(ports=ports, gateway_home=str(home), created_at="test")
    values = gateway_values(
        record,
        IdentityInput(
            home,
            REPO_ROOT,
            frozenset({"gateway", "agent-runner"}),
            {},
            occupied_ports=frozenset(),
            birth=birth_facts(),
        ),
    )
    assert values["MEIVA_CLUSTER_SECRET"]
    assert "MEIVA_DB_ADMIN_PASSWORD" not in values
    assert "MEIVA_RUNNER_DB_PASSWORD" not in values
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    write_configured_intent(home, record, values)
    # meiva:note
    # Pin the backup passphrase as the birth's claim publication does.
    passphrase.ensure_minted(home)
    # meiva:note
    # The config the commands read: the unit environment's values with the minted ones.
    plane = replace(
        unit_plane,
        cluster_secret=values["MEIVA_CLUSTER_SECRET"],
        db_url=values["MEIVA_DB_URL"],
        redis_url=values["MEIVA_REDIS_URL"],
        redis_admin_password=values["MEIVA_REDIS_ADMIN_PASSWORD"],
        events_channel=values["MEIVA_EVENTS_CHANNEL"],
        pgbouncer_enabled=True,
    )
    return ConfiguredSingleBox(home=home, record=record, values=values, plane=plane)


# meiva:note
# Birth records this session's leased roster, not whatever a kernel probe would hand out.
#
# This test used to fake `socket.socket` with a kernel that releases a port for reuse:
# that is how two closed probes could be handed one number and duplicate a slot. A leased
# number belongs to the session for its whole life (`base/host/env/tests/test_port_lease.py`
# proves that across processes), so the roster is distinct by construction — and the birth
# still holds no socket of its own.
def test_configured_roster_is_this_sessions_leased_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    distinct_free_ports: Callable[[int], list[int]],
    data_plane_config: DataPlaneConfig,
) -> None:
    home = (tmp_path / "home").resolve()
    point_home(monkeypatch, home)
    configured = configure_single_box_home(home, distinct_free_ports, data_plane_config)
    intent = read_intent(configured.home)
    assert intent is not None
    assert set(configured.record.ports) == set(cluster.FIXED_PORTS)
    assert len(set(configured.record.ports.values())) == len(cluster.FIXED_PORTS)
    assert set(configured.record.ports.values()) <= set(port_lease.leased_ports())
