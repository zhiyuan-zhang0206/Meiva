# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def service_snapshot() -> None:
    from base.packages.plugins.config_registration import read_config_image, read_service_config
    from base.packages.plugins.tests.test_service_config_snapshot import Config

    captured = Config(enabled=True, interval=42)
    assert read_service_config("fleet", Config) == captured
    assert read_config_image(Config, Path(sys.argv[2])).enabled is False


def service_carrier() -> None:
    from base.packages.plugins.config_registration import read_service_config
    from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
    from base.packages.plugins.tests.test_service_config_snapshot import Config

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
    assert not settings.has_domain("agent")
    assert validate_flag_key(flag) == flag
    with pytest.raises(FlagDomainUnavailable):
        read_declared_flag(flag, (flag,))


def failed_report_writer() -> None:
    from base.log import logger
    from base.packages.plugins.load_report import report_plugin_load_failure
    from base.telemetry import EventPipeline, init_telemetry
    from base.telemetry.delivery.receipts import Event

    error = RuntimeError("telemetry down")

    def unavailable(_events: list[Event]) -> None:
        raise error

    from base.log.tests.log_records import LogRecords

    records = LogRecords()
    sink = logger.add(lambda message: records.append(dict(message.record)))
    pipeline = EventPipeline(writer=unavailable, flush_interval_s=0.01)
    init_telemetry(pipeline=pipeline)
    try:
        report_plugin_load_failure("demo", RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="telemetry down") as failed:
            pipeline.sync()
        assert failed.value is error
        record = next(row for row in records if "failed to load" in row["message"])
        assert record["exception"] is not None
        assert record["exception"].type is RuntimeError
    finally:
        logger.remove(sink)
        with pytest.raises(RuntimeError) as stopped:
            pipeline.stop()
        assert stopped.value is error


def gateway_overlay() -> None:
    from base.config import settings
    from base.packages.plugin_providers import build_model_catalog
    from base.packages.plugins.config_registration import validate_config_overlay
    from base.packages.plugins.plugin_errors import InvalidConfigOverlay

    assert not settings.has_domain("agent")
    models = build_model_catalog().models
    if sys.argv[2] == "declaring-model":
        validate_config_overlay({"completion_notice_policy": "hourly"}, configs={}, models=models)
        with pytest.raises(InvalidConfigOverlay, match="completion_notice_policy"):
            validate_config_overlay(
                {"completion_notice_policy": "bogus"}, configs={}, models=models
            )
    elif sys.argv[2] == "validators":
        validate_config_overlay({"skills_to_expand_at_start": "a,b"}, configs={}, models=models)
        with pytest.raises(InvalidConfigOverlay, match="only accepts"):
            validate_config_overlay(
                {"eval_network_allowlist": ["web", "shell"]}, configs={}, models=models
            )
    else:
        raise ValueError("unknown gateway overlay case")


cases = {
    "service-snapshot": service_snapshot,
    "service-carrier": service_carrier,
    "unavailable-flag": unavailable_flag,
    "failed-report-writer": failed_report_writer,
    "gateway-overlay": gateway_overlay,
}
cases[sys.argv[1]]()
