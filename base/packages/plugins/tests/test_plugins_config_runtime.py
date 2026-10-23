from pathlib import Path

import pytest

from base.packages.plugins.enable_config import (
    DanglingPlugin,
    load,
    load_for_runtime,
    write_local,
)


def test_load_for_runtime_drops_dangling_and_reports_each_name(tmp_path: Path) -> None:
    write_local(
        {
            "plugins": {
                "real": {"enabled": True},
                "zulu": {"enabled": False},
                "alpha": {"enabled": True},
            }
        },
        home=tmp_path,
    )
    reported: list[tuple[str, BaseException]] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append((name, exc))

    config = load_for_runtime({"real"}, report=_capture, home=tmp_path)

    assert set(config.plugins) == {"real"}
    assert config.plugins["real"].enabled
    assert [name for name, _ in reported] == ["alpha", "zulu"]
    assert isinstance(reported[0][1], DanglingPlugin)


def test_load_for_runtime_reports_each_failed_load(tmp_path: Path) -> None:
    write_local(
        {"plugins": {"real": {"enabled": True}, "vanished": {"enabled": False}}},
        home=tmp_path,
    )
    reported: list[str] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append(name)

    load_for_runtime({"real"}, report=_capture, home=tmp_path)
    load_for_runtime({"real"}, report=_capture, home=tmp_path)

    assert reported == ["vanished", "vanished"]


def test_load_for_runtime_keeps_enabled_flags(tmp_path: Path) -> None:
    write_local({"plugins": {"real": {"enabled": False}}}, home=tmp_path)

    config = load_for_runtime({"real"}, home=tmp_path)

    assert config.plugins["real"].enabled is False


def test_strict_load_still_raises_on_dangling(tmp_path: Path) -> None:
    write_local({"plugins": {"vanished": {"enabled": True}}}, home=tmp_path)

    with pytest.raises(DanglingPlugin):
        load({"real"}, home=tmp_path)
