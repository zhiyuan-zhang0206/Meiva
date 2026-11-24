from __future__ import annotations

import dataclasses
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


def test_the_slice_carries_the_value_each_field_holds() -> None:
    owner = _owner()
    owner.set_field("events_maintenance_interval_seconds", 17.0)
    owner.set_field("timezone", "Asia/Shanghai")

    config = daemon.events_maintenance_config(owner)

    for name, value in dataclasses.asdict(config).items():
        flat: Any = owner.get_field(name)
        assert value == flat, name
    assert config.events_maintenance_interval_seconds == 17.0
    assert config.timezone == "Asia/Shanghai"


def test_two_entry_slices_remain_independent_at_each_lookup() -> None:
    first = _owner()
    second = _owner()
    first.set_field("events_maintenance_interval_seconds", 17.0)
    second.set_field("events_maintenance_interval_seconds", 23.0)

    def interval(owner: ConfigBoot) -> float:
        return daemon.events_maintenance_config(owner).events_maintenance_interval_seconds

    assert interval(first) == 17.0
    assert interval(second) == 23.0
    first.set_field("events_maintenance_interval_seconds", 31.0)
    assert interval(first) == 31.0
    assert interval(second) == 23.0


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
