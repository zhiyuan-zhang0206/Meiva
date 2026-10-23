from pathlib import Path

import pytest

from base.packages.plugins.enable_config import (
    DanglingPlugin,
    PluginEnableStore,
    load,
    load_for_runtime,
    write_local,
)

pytestmark = pytest.mark.usefixtures("unit_home")


@pytest.fixture
def store(unit_home: Path) -> PluginEnableStore:
    return PluginEnableStore(unit_home / "plugins_config.json")


def test_load_for_runtime_drops_dangling_and_reports_each_name(
    store: PluginEnableStore,
) -> None:
    write_local(
        {
            "plugins": {
                "real": {"enabled": True},
                "zulu": {"enabled": False},
                "alpha": {"enabled": True},
            }
        }
    )
    reported: list[tuple[str, BaseException]] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append((name, exc))

    config = store.load_for_runtime({"real"}, report=_capture)

    assert set(config.plugins) == {"real"}
    assert config.plugins["real"].enabled
    assert [name for name, _ in reported] == ["alpha", "zulu"]
    assert isinstance(reported[0][1], DanglingPlugin)


def test_load_for_runtime_reports_each_failed_load(
    store: PluginEnableStore,
) -> None:
    write_local({"plugins": {"real": {"enabled": True}, "vanished": {"enabled": False}}})
    reported: list[str] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append(name)

    store.load_for_runtime({"real"}, report=_capture)
    store.load_for_runtime({"real"}, report=_capture)

    assert reported == ["vanished", "vanished"]


def test_load_for_runtime_keeps_enabled_flags() -> None:
    write_local({"plugins": {"real": {"enabled": False}}})

    config = load_for_runtime({"real"})

    assert config.plugins["real"].enabled is False


def test_strict_load_still_raises_on_dangling() -> None:
    write_local({"plugins": {"vanished": {"enabled": True}}})

    with pytest.raises(DanglingPlugin):
        load({"real"})
