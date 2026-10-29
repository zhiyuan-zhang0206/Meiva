# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.paths import meiva_home


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = Field(default=True, strict=True)
    interval: float = Field(default=300, strict=True)


def service_snapshot() -> None:
    from base.packages.plugins.config_registration import read_config_image, read_service_config

    captured = Config(enabled=True, interval=42)
    assert read_service_config("fleet", Config) == captured
    assert read_config_image(Config, Path(sys.argv[2])).enabled is False


def service_carrier() -> None:
    from base.packages.plugins.config_registration import read_service_config
    from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError

    expected = sys.argv[2]
    if expected == "absent":
        assert read_service_config("fleet", Config) is None
        return
    errors = {"InvalidConfigData": InvalidConfigData, "SchemaDriftError": SchemaDriftError}
    with pytest.raises(errors[expected]):
        read_service_config("fleet", Config)


def unavailable_flag() -> None:
    from base.config import settings
    from base.packages.plugins.flags import (
        FlagDomainUnavailable,
        read_declared_flag,
        validate_flag_key,
    )

    flag = "agent.completion_notice_policy"
    assert settings.profile == "gateway"
    assert not settings.has_domain("agent")
    assert validate_flag_key(flag) == flag
    with pytest.raises(FlagDomainUnavailable):
        read_declared_flag(flag, (flag,))


def failed_report_writer() -> None:
    from base.log import logger
    from base.packages.plugins.load_report import report_plugin_load_failure
    from base.telemetry import Event, EventPipeline, init_telemetry

    error = RuntimeError("telemetry down")
    attempted: list[Event] = []

    def unavailable(events: list[Event]) -> None:
        attempted.extend(events)
        raise error

    from base.log.tests.log_records import LogRecords

    records = LogRecords()
    sink = logger.add(lambda message: records.append(dict(message.record)))
    pipeline = EventPipeline(writer=unavailable, flush_interval_s=0.01)
    init_telemetry(home=meiva_home(), pipeline=pipeline)
    try:
        report_plugin_load_failure("demo", RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="telemetry down") as failed:
            pipeline.sync()
        assert failed.value is error
        events = [event for event in attempted if event.event_name == "plugin_load_failed"]
        assert len(events) == 1
        assert events[0].category == "telemetry"
        assert events[0].level == "error"
        assert events[0].attributes == {"plugin": "demo", "error": "RuntimeError: boom"}
        record = next(row for row in records if "failed to load" in row["message"])
        assert record["exception"] is not None
        assert record["exception"].type is RuntimeError
    finally:
        logger.remove(sink)
        with pytest.raises(RuntimeError) as stopped:
            pipeline.stop()
        assert stopped.value is error


cases = {
    "service-snapshot": service_snapshot,
    "service-carrier": service_carrier,
    "unavailable-flag": unavailable_flag,
    "failed-report-writer": failed_report_writer,
}
if __name__ == "__main__":
    cases[sys.argv[1]]()
