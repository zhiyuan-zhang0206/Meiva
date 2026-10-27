from pathlib import Path
from typing import cast

import pytest

from base.log.tests.log_records import LogRecords
from base.packages.plugin_config_admin import update_all_disk_images
from base.packages.plugins import enable_config
from base.packages.plugins.config_registration import disk_image_path
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
from base.paths import meiva_home
from meiva_builtins.plugins.meiva_memory import services
from meiva_builtins.plugins.meiva_memory.default_config import MemoryConfig
from services.supervision.meiva_root.inputs import InputChangedError, InputSeal
from services.supervision.meiva_root_glue.manifests import build_units
from services.supervision.roster import plugin_services as roster_plugins
from services.supervision.roster.service_spec import ServiceSpec


def _indexer() -> ServiceSpec:
    return next(
        spec
        for spec in roster_plugins.plugin_services(home=meiva_home())
        if spec.session == "memory-indexer"
    )


def test_missing_image_runs_indexer_without_writing_home(unit_home: Path) -> None:
    spec = _indexer()
    assert spec.capabilities == frozenset({"gateway"})
    assert spec.gate is not None and spec.gate() is None
    assert spec.config_inputs == (unit_home / "configs" / "meiva_memory" / "config.json",)
    assert not (unit_home / "configs").exists()


def test_host_switch_disables_indexer(unit_home: Path) -> None:
    image = disk_image_path("meiva_memory", home=unit_home)
    image.parent.mkdir(parents=True)
    image.write_text('{"indexer_enabled": false}\n')
    spec = _indexer()
    assert spec.gate is not None
    assert spec.gate() == "disabled (meiva_memory.indexer_enabled off)"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not json", InvalidConfigData),
        ("[]", InvalidConfigData),
        ("{}", SchemaDriftError),
        ('{"indexer_enabled": "false"}', InvalidConfigData),
    ],
)
def test_invalid_image_fails_composition_before_a_gate_can_fail_open(
    unit_home: Path, content: str, error: type[Exception], loguru_records: LogRecords
) -> None:
    image = disk_image_path("meiva_memory", home=unit_home)
    image.parent.mkdir(parents=True)
    image.write_text(content)
    with pytest.raises(error):
        services.services(home=unit_home)
    with pytest.raises(error):
        roster_plugins.plugin_services(home=unit_home)
    failed = [r for r in loguru_records if "failed to load" in r["message"]]
    assert [r["message"].split()[2] for r in failed] == ["meiva_memory"]
    assert image.read_text() == content


def test_first_converge_materializes_and_seals_the_declared_input(unit_home: Path) -> None:
    before = _indexer()
    assert before.gate is not None and before.gate() is None
    assert not disk_image_path("meiva_memory", home=unit_home).exists()
    update = update_all_disk_images(
        plugin_dirs=enable_config.discover_plugins(home=unit_home), home=unit_home
    )
    memory = next(entry for entry in update.entries if entry.name == "meiva_memory")
    assert memory.status == "updated"
    image = disk_image_path("meiva_memory", home=unit_home)
    assert image.read_text() == MemoryConfig().model_dump_json(indent=2) + "\n"
    spec = _indexer()
    assert spec.gate is not None and spec.gate() is None
    units = build_units(
        [spec], capabilities={"gateway"}, repo_root=Path(__file__).parents[4], home=unit_home
    )
    inputs = cast("list[object]", units[0]["inputs"])
    assert len(inputs) == 1
    seal = InputSeal.from_mapping(inputs[0])
    image.write_text('{"indexer_enabled": false}\n')
    with pytest.raises(InputChangedError):
        seal.require_unchanged()
    disabled = services.services(home=unit_home)[0]
    assert disabled.gate is not None
    assert disabled.gate() == "disabled (meiva_memory.indexer_enabled off)"
