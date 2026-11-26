from __future__ import annotations

from dataclasses import replace

import pytest

from services.derived.embeddinggemma.launch import EmbeddingGemmaSettings
from services.supervision.roster.gate_plan import (
    BridgeFact,
    BrowserFact,
    CollectorFact,
    ComputerFact,
    EmbeddingGemmaFact,
    FlagFact,
    GateFact,
    GateKind,
    StationFact,
    gate_kind,
    gate_reason,
)


@pytest.mark.parametrize(
    ("session", "kind"),
    [
        ("heartbeat", "flag"),
        ("delivery-watchdog", "flag"),
        ("browser", "browser"),
        ("browser-mcp", "browser"),
        ("computer-mcp", "computer-mcp"),
        ("loki", "station"),
        ("prometheus", "station"),
        ("grafana", "station"),
        ("otel-collector", "collector"),
        ("redis-bridge", "bridge"),
        ("embeddinggemma", "embeddinggemma"),
        ("gateway", "none"),
        ("ops", "none"),
    ],
)
def test_each_session_answers_to_one_gate(session: str, kind: GateKind) -> None:
    assert gate_kind(session) == kind


def test_an_ungated_session_is_never_gated_out() -> None:
    assert gate_reason("gateway", None) is None


def test_a_gated_session_without_its_fact_is_refused() -> None:
    with pytest.raises(ValueError, match="needs its gate fact"):
        gate_reason("browser", None)


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        ("heartbeat", "disabled (MEIVA_HEARTBEAT_ENABLED off)"),
        ("delivery-watchdog", "disabled (MEIVA_DELIVERY_WATCHDOG_ENABLED off)"),
    ],
)
def test_a_flag_session_names_its_flag_when_off_and_runs_when_on(
    session: str, expected: str
) -> None:
    assert gate_reason(session, FlagFact(enabled=False)) == expected
    assert gate_reason(session, FlagFact(enabled=True)) is None


def test_a_flag_fact_for_another_session_is_refused() -> None:
    with pytest.raises(ValueError, match="no settings-flag gate"):
        gate_reason("browser", FlagFact(enabled=True))


def test_the_browser_family_is_gated_by_its_flag_before_the_platform() -> None:
    assert (
        gate_reason("browser", BrowserFact(enabled=False, incapability="no display"))
        == "disabled (MEIVA_BROWSER_ENABLED off)"
    )
    assert gate_reason("browser-mcp", BrowserFact(enabled=True, incapability="no display")) == (
        "no display"
    )
    assert gate_reason("browser", BrowserFact(enabled=True, incapability=None)) is None


def test_computer_mcp_is_gated_by_the_helper_flag_before_the_platform() -> None:
    assert gate_reason("computer-mcp", ComputerFact(helper_enabled=False, incapability="x")) == (
        "disabled (MEIVA_PERMISSIONS_HELPER_ENABLED off)"
    )
    assert gate_reason("computer-mcp", ComputerFact(helper_enabled=True, incapability="x")) == "x"
    assert gate_reason("computer-mcp", ComputerFact(helper_enabled=True, incapability=None)) is None
    assert (
        gate_reason(
            "computer-mcp",
            ComputerFact(helper_enabled=False, incapability="x", has_remote_targets=True),
        )
        is None
    )


def test_the_native_backends_run_only_on_the_observability_station() -> None:
    assert gate_reason("loki", StationFact(is_station=True)) is None
    assert gate_reason("grafana", StationFact(is_station=False)) == (
        "this home is not an observability station"
    )


def test_the_collector_names_the_override_that_would_allow_it() -> None:
    assert gate_reason("otel-collector", CollectorFact(allowed=True)) is None
    reason = gate_reason("otel-collector", CollectorFact(allowed=False))
    assert reason is not None
    assert "lgtm-host" in reason
    assert "MEIVA_TELEMETRY_OTLP_ENDPOINT" in reason


@pytest.mark.parametrize(
    ("fact", "expected"),
    [
        (
            BridgeFact(unconfigured="redis ingress is not declared", has_endpoint=False),
            "redis ingress is not declared",
        ),
        (
            BridgeFact(unconfigured=None, has_endpoint=False),
            "no off-box Redis ingress on this host",
        ),
        (BridgeFact(unconfigured=None, has_endpoint=True), None),
    ],
)
def test_the_relay_is_gated_by_its_declaration_and_its_endpoint(
    fact: GateFact, expected: str | None
) -> None:
    assert gate_reason("redis-bridge", fact) == expected


_SHA = "e7a8a2204b91e0f96e92960e84a09a89212e1633dcb7575a9bf3378b4df77f4c"


def _gemma(host: str, *, self_host: str, **changes: object) -> EmbeddingGemmaFact:
    base = EmbeddingGemmaSettings(
        host=host,
        port=9379,
        device="cpu",
        revision=_SHA,
        model=None,
        model_path="/models/model.litertlm",
        cache_dir="/models/cache",
        python="",
        litert_path="",
        bind_host="0.0.0.0",  # noqa: S104
        max_input_length=8192,
        api_key_configured=False,
    )
    return EmbeddingGemmaFact(settings=replace(base, **changes), self_host=self_host)


def test_embeddinggemma_runs_only_on_the_machine_whose_address_is_the_placement_host() -> None:
    assert gate_reason("embeddinggemma", _gemma("10.64.0.7", self_host="10.64.0.7")) is None
    assert gate_reason("embeddinggemma", _gemma("10.64.0.9", self_host="10.64.0.7")) == (
        "EmbeddingGemma is hosted on 10.64.0.9, not on this machine"
    )
    assert gate_reason("embeddinggemma", _gemma("", self_host="10.64.0.7")) == (
        "no managed EmbeddingGemma service (MEIVA_EMBEDDINGGEMMA_HOST is empty)"
    )


def test_a_placement_naming_this_machine_needs_a_revision_the_model_can_be_verified_against() -> (
    None
):
    for revision in (None, "", "f9972ac22e696a105c482202646979e33e4b5a1e"):
        reason = gate_reason("embeddinggemma", _gemma("h", self_host="h", revision=revision))
        assert reason is not None
        assert "64-hex SHA256" in reason


@pytest.mark.parametrize(
    "changes",
    [
        {"bind_host": ""},
        {"max_input_length": 0},
        {"max_input_length": -1},
        {"port": 0},
        {"port": 70000},
        {"model_path": ""},
        {"cache_dir": " "},
        {"model": ""},
        {"device": "cuda"},
    ],
)
def test_an_invalid_value_on_the_hosting_machine_is_a_refusal_not_an_exception(
    changes: dict[str, object],
) -> None:
    reason = gate_reason("embeddinggemma", _gemma("h", self_host="h", **changes))
    assert reason is not None
    assert "invalid EmbeddingGemma configuration" in reason or "device" in reason


def test_an_invalid_value_is_never_judged_on_another_machine() -> None:
    fact = _gemma("elsewhere", self_host="h", bind_host="", max_input_length=0)
    assert gate_reason("embeddinggemma", fact) == (
        "EmbeddingGemma is hosted on elsewhere, not on this machine"
    )
