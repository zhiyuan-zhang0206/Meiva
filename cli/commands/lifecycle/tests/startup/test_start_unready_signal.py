"""A non-critical service that misses its readiness window emits `service_start_unready`."""

from __future__ import annotations

from collections.abc import Generator

import pytest

from base.telemetry.tests.capture.event_capture import EmittedEvents, FollowedAttributes
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.lifecycle.root_driver import LaunchOutcome
from cli.commands.lifecycle.start import readiness_verdict
from cli.commands.probe import ReadinessWait, report_non_critical_unready_services
from services.supervision.roster.service_spec import GATEWAY, ServiceSpec


def _spec(session: str) -> ServiceSpec:
    return ServiceSpec(session=session, cmd="x", capabilities=GATEWAY, requires_db=True)


def _wait(
    *, unready: tuple[ServiceSpec, ...] = (), non_critical: tuple[ServiceSpec, ...] = ()
) -> ReadinessWait:
    return ReadinessWait(unready, 0.0, sessions_gone=False, non_critical_unready=non_critical)


@pytest.fixture
def emitted(emitted_events: EmittedEvents) -> Generator[FollowedAttributes]:
    """The attributes of every `service_start_unready` event. After the test, every event that was
    emitted must be that event, at warning level."""
    yield emitted_events.following("service_start_unready")
    for event in emitted_events:
        assert (event.category, event.event_name) == ("telemetry", "service_start_unready")
        assert event.level == "warning"


def test_one_event_per_non_critical_service(emitted: FollowedAttributes) -> None:
    report_non_critical_unready_services((_spec("labeler"), _spec("watcher")))

    assert emitted == [{"service": "meiva-labeler"}, {"service": "meiva-watcher"}]


def test_readiness_verdict_emits_for_non_critical_only_and_allows_the_start(
    emitted: FollowedAttributes,
) -> None:
    launch = LaunchOutcome((_spec("labeler"),), ())
    wait = _wait(non_critical=(_spec("labeler"),))

    assert readiness_verdict(launch, wait, ready_timeout=180) is None
    assert emitted == [{"service": "meiva-labeler"}]


def test_a_ready_start_emits_nothing(emitted: FollowedAttributes) -> None:
    assert readiness_verdict(LaunchOutcome((), ()), _wait(), ready_timeout=180) is None
    assert emitted == []


@pytest.mark.parametrize("failed, expected", [("meiva-browser-mcp", None), ("meiva-gateway", 4)])
def test_only_core_launch_failures_block_serving(
    emitted: FollowedAttributes, failed: str, expected: int | None
) -> None:
    launch = LaunchOutcome((), (failed,))

    assert readiness_verdict(launch, _wait(), ready_timeout=180) == expected


def test_an_unready_core_service_is_named_on_stderr_and_fails(
    emitted: FollowedAttributes, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = _wait(unready=(_spec("gateway"),))

    assert readiness_verdict(LaunchOutcome((_spec("gateway"),), ()), wait, ready_timeout=180) == 4
    assert "never became ready within 180s" in capsys.readouterr().err
    assert emitted == []
