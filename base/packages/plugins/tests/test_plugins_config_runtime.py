from pathlib import Path

import pytest

from base.log.tests.log_records import LogRecords
from base.packages.plugins.enable_config import (
    DanglingPlugin,
    load,
    load_for_runtime,
    write_local,
)
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


def test_load_for_runtime_drops_dangling_and_reports_each_name(
    tmp_path: Path, emitted_events: EmittedEvents, loguru_records: LogRecords
) -> None:
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
    config = load_for_runtime({"real"}, home=tmp_path)

    assert set(config.plugins) == {"real"}
    assert config.plugins["real"].enabled
    reported = emitted_events.attributes("plugin_load_failed")
    assert [event["plugin"] for event in reported] == ["alpha", "zulu"]
    failures = [record for record in loguru_records if record["level"].name == "ERROR"]
    assert len(failures) == 2
    assert all(record["exception"].type is DanglingPlugin for record in failures)


def test_load_for_runtime_reports_each_failed_load(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    write_local(
        {"plugins": {"real": {"enabled": True}, "vanished": {"enabled": False}}},
        home=tmp_path,
    )
    load_for_runtime({"real"}, home=tmp_path)
    load_for_runtime({"real"}, home=tmp_path)

    assert [event["plugin"] for event in emitted_events.attributes("plugin_load_failed")] == [
        "vanished",
        "vanished",
    ]


def test_load_for_runtime_keeps_enabled_flags(tmp_path: Path) -> None:
    write_local({"plugins": {"real": {"enabled": False}}}, home=tmp_path)

    config = load_for_runtime({"real"}, home=tmp_path)

    assert config.plugins["real"].enabled is False


def test_strict_load_still_raises_on_dangling(tmp_path: Path) -> None:
    write_local({"plugins": {"vanished": {"enabled": True}}}, home=tmp_path)

    with pytest.raises(DanglingPlugin):
        load({"real"}, home=tmp_path)
