from __future__ import annotations

from base.log import logger
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.plugins import activation


def _emitted_tuples(loguru_records: LogRecords) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for record in loguru_records.events(activation.PLUGIN_ACTIVATION_EVENT):
        extra = record["extra"]
        out.append(
            (
                str(extra["plugin"]),
                str(extra["surface"]),
                str(extra["identifier"]),
                str(extra["detail"]),
            )
        )
    return out


def test_records_an_attributed_firing(loguru_records: LogRecords) -> None:
    activation.record(
        "meiva_syntax_fix", "hooks", "before_exec", detail="wrote messages", model="m"
    )

    assert _emitted_tuples(loguru_records) == [
        ("meiva_syntax_fix", "hooks", "before_exec", "wrote messages")
    ]


def test_unattributed_firing_records_nothing(loguru_records: LogRecords) -> None:
    activation.record(None, "hooks", "before_llm", detail="wrote messages", model="m")

    assert _emitted_tuples(loguru_records) == []


def test_event_carries_the_model_in_force(loguru_records: LogRecords) -> None:
    activation.emit("meiva_code", "sdkWraps", "files.read", "inner_calls=0", "deepseek-flash")

    logged = loguru_records.events(activation.PLUGIN_ACTIVATION_EVENT)
    assert len(logged) == 1
    payload = logged[0]["extra"]
    assert payload["event"] == activation.PLUGIN_ACTIVATION_EVENT
    assert payload["plugin"] == "meiva_code"
    assert payload["surface"] == "sdkWraps"
    assert payload["identifier"] == "files.read"
    assert payload["detail"] == "inner_calls=0"
    assert payload["model"] == "deepseek-flash"


def test_emit_failure_is_swallowed() -> None:
    def _raise_sink(_message: object) -> None:
        raise RuntimeError("sink down")

    sink_id = logger.add(_raise_sink, catch=False)
    try:
        activation.record("meiva_memory", "hooks", "after_exec", detail="wrote messages", model="m")
    finally:
        logger.remove(sink_id)


def test_registered_event_name_is_on_contract() -> None:
    from base.events.contract import EVENTS, payload_keys

    spec = EVENTS[activation.PLUGIN_ACTIVATION_EVENT]
    assert spec.category == "telemetry"
    assert set(payload_keys(activation.PLUGIN_ACTIVATION_EVENT)) == {
        "plugin",
        "surface",
        "identifier",
        "detail",
        "model",
    }
