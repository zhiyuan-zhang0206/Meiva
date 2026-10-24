from __future__ import annotations

import pytest

from services.supervision.roster.gate_plan import (
    BridgeFact,
    BrowserFact,
    CollectorFact,
    ComputerFact,
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
        ("im-bridge", "flag"),
        ("browser", "browser"),
        ("browser-mcp", "browser"),
        ("computer-mcp", "computer-mcp"),
        ("loki", "station"),
        ("prometheus", "station"),
        ("grafana", "station"),
        ("otel-collector", "collector"),
        ("redis-bridge", "bridge"),
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
        ("im-bridge", "disabled (MEIVA_IM_BRIDGE_ENABLED off)"),
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
