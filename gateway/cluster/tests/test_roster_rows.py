# meiva:note
# The roster row shapers: pure functions of a machines-table row and the facts read for it.

from __future__ import annotations

from datetime import UTC, datetime

from base.api_contracts.status import SchemaMismatchStatus
from gateway.cluster.roster.roster_rows import (
    LocalFacts,
    RosterRowSpec,
    identity_mismatch_status,
    local_status,
    offline_status,
    reachable_unknown_status,
    reported_status,
    service_item,
)
from ops.cluster_status import ClusterStatus

_UP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _row(
    name: str = "runner-a", *roles: str, url: str | None = "http://runner-a:8113"
) -> RosterRowSpec:
    return RosterRowSpec(
        name=name,
        gateway_url=url,
        role=list(roles or ("agent-runner",)),
        up_since_at=_UP,
        description="the runner",
        stopped_at=None,
        is_staging=True,
    )


def _status(name: str = "runner-a", **extra: object) -> ClusterStatus:
    return ClusterStatus.model_validate(
        {
            "machine_name": name,
            "serve_gateway": False,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
            "running_sha": "def456",
            "shell_count": 3,
            **extra,
        }
    )


def test_the_three_abnormal_rows_say_which_abnormal_state_they_are() -> None:
    row = _row()

    unknown = reachable_unknown_status(row)
    offline = offline_status(row)
    mismatch = identity_mismatch_status(row)

    assert (unknown.online, unknown.paused, unknown.identity_mismatch) == (True, None, False)
    assert (offline.online, offline.paused, offline.identity_mismatch) == (False, None, False)
    assert (mismatch.online, mismatch.paused, mismatch.identity_mismatch) == (False, None, True)
    for shaped in (unknown, offline, mismatch):
        assert shaped.description == "the runner" and shaped.is_staging is True
        assert shaped.serve_agent_runner and not shaped.serve_gateway
        assert shaped.head_sha is None


def test_a_missing_gateway_url_shows_as_an_empty_string() -> None:
    assert offline_status(_row(url=None)).gateway_url == ""


def test_capabilities_follow_the_role_column() -> None:
    station = offline_status(_row("s", "gateway", "observability-station"))

    assert (station.serve_gateway, station.serve_agent_runner) == (True, False)
    assert station.serve_observability_station is True


def test_a_reported_row_carries_the_hosts_own_answer() -> None:
    probed = _status(paused=True, paused_reason="startup", agent_host_online=True)

    shaped = reported_status(_row(), probed, observed_at=_UP)

    assert shaped.online is True and shaped.paused is True and shaped.paused_reason == "startup"
    assert (shaped.head_sha, shaped.running_sha, shaped.shell_count) == ("abc123", "def456", 3)
    assert shaped.agent_host_online is True
    assert shaped.observed_at == _UP


def test_an_answer_under_another_name_is_an_identity_mismatch_not_a_green_row() -> None:
    shaped = reported_status(_row(), _status("somewhere-else"), observed_at=None)

    assert shaped.identity_mismatch is True and shaped.online is False
    # meiva:note
    # the impostor's data is not picked up
    assert shaped.head_sha is None


def test_the_local_row_is_online_and_paused_by_the_serving_gate() -> None:
    mismatch = SchemaMismatchStatus(kind="schema-behind-code", machine="gw", detail="d")
    facts = LocalFacts(
        paused_reason="startup",
        head_sha="abc123",
        running_sha="fff000",
        schema_mismatch=mismatch,
        resource=None,
    )

    shaped = local_status(_row("gw", "gateway"), facts)

    assert (shaped.online, shaped.paused, shaped.paused_reason) == (True, True, "startup")
    assert (shaped.head_sha, shaped.running_sha) == ("abc123", "fff000")
    assert shaped.schema_mismatch == mismatch
    assert shaped.shell_count == 0 and shaped.agent_host_online is None


def test_a_serving_local_gateway_is_not_paused() -> None:
    facts = LocalFacts(None, None, None, None, None)

    shaped = local_status(_row("gw", "gateway"), facts)

    assert (shaped.paused, shaped.paused_reason) == (False, None)


def test_a_service_line_says_why_a_daemon_is_not_online() -> None:
    assert service_item("labeler", "Labeler", alive=True, pid=7).detail is None
    assert service_item("labeler", "Labeler", alive=False, pid=7).detail == (
        "pidfile exists but process is dead"
    )
    assert service_item("labeler", "Labeler", alive=False, pid=None).detail == "pidfile not found"
