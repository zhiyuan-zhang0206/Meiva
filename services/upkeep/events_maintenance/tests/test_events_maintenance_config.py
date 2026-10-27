from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from base.config import ConfigBoot
from base.db.code_version_gate import ProcessDbGate
from base.paths import meiva_home
from services.upkeep.events_maintenance import config as maintenance_config
from services.upkeep.events_maintenance import daemon


def _owner() -> ConfigBoot:
    owner = ConfigBoot()
    owner.read_process_environment()
    return owner


def test_the_slice_carries_the_value_each_field_holds(tmp_path: Path) -> None:
    owner = _owner()
    owner.set_field("events_resolution_burst_threshold", 17)
    owner.set_field("timezone", "Asia/Shanghai")

    config = daemon.events_maintenance_config(owner, mirror_dir=tmp_path)

    for field in dataclasses.fields(config):
        if field.name == "mirror_dir":
            continue
        flat: Any = owner.get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert config.events_resolution_burst_threshold == 17
    assert config.timezone == "Asia/Shanghai"
    assert config.mirror_dir == tmp_path


def test_two_entry_slices_remain_independent_at_each_lookup(tmp_path: Path) -> None:
    first = _owner()
    second = _owner()
    first.set_field("events_resolution_burst_threshold", 17)
    second.set_field("events_resolution_burst_threshold", 23)

    def threshold(owner: ConfigBoot) -> int:
        return daemon.events_maintenance_config(
            owner, mirror_dir=tmp_path
        ).events_resolution_burst_threshold

    assert threshold(first) == 17
    assert threshold(second) == 23
    first.set_field("events_resolution_burst_threshold", 31)
    assert threshold(first) == 31
    assert threshold(second) == 23


def test_database_slice_uses_the_supplied_owner_at_each_factory_call(
    database_gate: ProcessDbGate,
) -> None:
    first = _owner()
    second = _owner()
    first.set_field("db_pool_max_size", 13)
    second.set_field("db_pool_max_size", 19)

    def pool_size(owner: ConfigBoot) -> int:
        database = maintenance_config.events_maintenance_db(
            owner, gate=database_gate, local_host=lambda: "127.0.0.1", home=meiva_home()
        )
        with database.pool() as pool:
            return pool.max_size

    sizes = [pool_size(first), pool_size(second)]
    first.set_field("db_pool_max_size", 23)
    sizes.append(pool_size(first))

    assert sizes == [13, 19, 23]
