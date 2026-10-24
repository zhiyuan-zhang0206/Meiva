from pathlib import Path

from base.config import ConfigBoot
from services.backup.walg import config as walg_config


def test_walg_readers_follow_their_own_live_config_boot(tmp_path: Path) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("walg_config_file", tmp_path / "first")
    second.set_field("walg_config_file", tmp_path / "second")

    def a() -> Path | None:
        return first.view.walg.walg_config_file

    def b() -> Path | None:
        return second.view.walg.walg_config_file

    assert walg_config.configured_path(path_reader=a) == tmp_path / "first"
    assert walg_config.configured_path(path_reader=b) == tmp_path / "second"
    first.set_field("walg_config_file", tmp_path / "updated")
    assert walg_config.configured_path(path_reader=a) == tmp_path / "updated"
    assert walg_config.configured_path(path_reader=b) == tmp_path / "second"
    first.set_field("walg_config_file", None)
    assert not walg_config.enabled(path_reader=a)
    assert walg_config.enabled(path_reader=b)
