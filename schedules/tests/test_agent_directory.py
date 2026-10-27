# meiva:note
# Finding and waking agents.
#
# Which agent a label names and who a schedule reports to are decided from values
# (`plan_directory_page`, `report_recipient`, `parse_schedule_id`). The SDK calls that read the
# pages and send the messages run against a gateway on a loopback port that serves the
# directory pages a test scripts and records every request, so each test asserts what the SDK
# sent: the page cursors it followed, the route it called for a terminated, a live and a missing
# agent, and the idempotency key a spawn carries.

from __future__ import annotations

from uuid import UUID

import pytest

from base.agents.context import MeivaContext
from meiva.gateway_client import use_client
from schedules.jobs.agent_directory import (
    AgentDispatch,
    ensure_agent,
    iter_agents,
    notify_report_agent,
    report_agent_id,
)
from schedules.plans.directory_plan import (
    Found,
    ListedAgent,
    NextPage,
    NotFound,
    ReportRecipient,
    plan_directory_page,
    report_recipient,
)
from schedules.plans.schedule_identity import parse_schedule_id
from schedules.tests.support.gateway_directory import (
    AgentRowSpec,
    DirectoryPage,
    GatewayRequest,
    serve_gateway,
)

_LEAD = ReportRecipient("probe-schedule", "MEIVA_PROBE_REPORT_AGENT", "lead", None)


def _agents(*rows: tuple[int, str | None, bool]) -> tuple[ListedAgent, ...]:
    return tuple(ListedAgent(agent_id, label, terminated) for agent_id, label, terminated in rows)


def test_an_exact_label_on_the_page_is_found_with_its_liveness() -> None:
    page = _agents((300, "steward-old", False), (200, "steward", True), (100, "steward", False))
    assert plan_directory_page(page, 100, "steward") == Found(200, True)


def test_a_label_that_only_contains_the_wanted_one_is_not_a_match() -> None:
    page = _agents((300, "steward-old", False), (250, "old-steward", False), (240, None, False))
    assert plan_directory_page(page, None, "steward") == NotFound()


def test_a_page_without_the_label_continues_at_its_cursor() -> None:
    assert plan_directory_page(_agents((300, "other", False)), 300, "steward") == NextPage(300)


def test_an_empty_last_page_ends_the_search() -> None:
    assert plan_directory_page((), None, "steward") == NotFound()


def test_the_first_exact_label_wins_even_when_the_cursor_continues() -> None:
    page = _agents((200, "steward", False))
    assert plan_directory_page(page, 150, "steward") == Found(200, False)


def test_a_blank_operator_variable_means_the_label_lookup() -> None:
    expected = ReportRecipient("c9", "MEIVA_X", "lead", None)
    assert report_recipient("c9", "MEIVA_X", "lead", None) == expected
    assert report_recipient("c9", "MEIVA_X", "lead", "  ") == expected


def test_an_operator_variable_names_the_agent() -> None:
    assert report_recipient("c9", "MEIVA_X", "lead", "17") == ReportRecipient(
        "c9", "MEIVA_X", "lead", 17
    )


@pytest.mark.parametrize(
    ("configured", "message"),
    [
        ("not-an-id", "MEIVA_X must be a numeric agent id"),
        ("1.5", "MEIVA_X must be a numeric agent id"),
        ("0", "MEIVA_X must be a positive agent id"),
        ("-4", "MEIVA_X must be a positive agent id"),
    ],
)
def test_a_malformed_operator_variable_stops_the_schedule_instead_of_falling_back(
    configured: str, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        report_recipient("c9", "MEIVA_X", "lead", configured)


def test_the_schedule_id_is_a_positive_integer() -> None:
    assert parse_schedule_id("12") == 12


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "MEIVA_SCHEDULE_ID is not set"),
        ("", "MEIVA_SCHEDULE_ID is not set"),
        ("abc", "must be an integer"),
        ("0", "schedule_id must be positive, got 0"),
        ("-3", "schedule_id must be positive, got -3"),
    ],
)
def test_a_missing_or_malformed_schedule_id_is_refused(raw: str | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_schedule_id(raw)


def _steward_pages() -> tuple[DirectoryPage, ...]:
    return (
        DirectoryPage(
            "steward", None, (AgentRowSpec(300, "steward-old", "idling"),), next_cursor=300
        ),
        DirectoryPage(
            "steward",
            300,
            (
                AgentRowSpec(200, "steward", "terminated"),
                AgentRowSpec(100, "steward", "idling"),
            ),
            next_cursor=100,
        ),
    )


def test_a_terminated_agent_found_on_a_later_page_is_resurrected_with_the_prompt(
    sdk_environment: MeivaContext,
) -> None:
    with serve_gateway(_steward_pages()) as gateway, gateway.client() as client, use_client(client):
        dispatch = ensure_agent("steward", "work")

    assert dispatch == AgentDispatch(200, "resurrected")
    assert gateway.directory_cursors() == [None, "300"]
    assert gateway.requests[-1] == GatewayRequest(
        "POST",
        "/api/agents/200/resurrect",
        {},
        {"prompt": "work", "resurrected_by": "agent:1"},
    )


def test_a_live_agent_is_messaged_instead_of_woken(sdk_environment: MeivaContext) -> None:
    pages = (DirectoryPage("steward", None, (AgentRowSpec(200, "steward", "running"),), None),)
    with serve_gateway(pages) as gateway, gateway.client() as client, use_client(client):
        dispatch = ensure_agent("steward", "work")

    assert dispatch == AgentDispatch(200, "messaged")
    [directory, message] = gateway.requests
    assert directory.path == "/api/agents"
    assert message.path == "/api/agents/200/messages"
    assert message.body["content"] == "work"
    assert message.body["source"] == "agent:1"


def test_a_missing_agent_is_spawned_once_every_page_is_exhausted(
    sdk_environment: MeivaContext,
) -> None:
    pages = (
        DirectoryPage("steward", None, (AgentRowSpec(300, "steward-old"),), next_cursor=300),
        DirectoryPage("steward", 300, (), next_cursor=None),
    )
    with serve_gateway(pages) as gateway, gateway.client() as client, use_client(client):
        dispatch = ensure_agent("steward", "work")

    assert dispatch == AgentDispatch(gateway.spawned_agent_id, "spawned")
    assert gateway.directory_cursors() == [None, "300"]
    spawn = gateway.requests[-1]
    assert spawn.path == "/api/keyed/v1/agents"
    assert spawn.body["prompt"] == "work"
    assert spawn.body["label"] == "steward"
    assert str(UUID(spawn.headers["idempotency-key"])) == spawn.headers["idempotency-key"]


def test_the_report_recipient_can_sit_beyond_the_first_page(sdk_environment: MeivaContext) -> None:
    pages = (
        DirectoryPage("lead", None, (AgentRowSpec(31, "another", "running"),), next_cursor=31),
        DirectoryPage("lead", 31, (AgentRowSpec(17, "lead", "terminated"),), next_cursor=None),
    )
    with serve_gateway(pages) as gateway, gateway.client() as client, use_client(client):
        assert report_agent_id(_LEAD) == 17

    assert gateway.directory_cursors() == [None, "31"]


def test_an_operator_named_recipient_needs_no_directory_read(sdk_environment: MeivaContext) -> None:
    named = ReportRecipient("probe-schedule", "MEIVA_PROBE_REPORT_AGENT", "lead", 42)
    with serve_gateway() as gateway, gateway.client() as client, use_client(client):
        assert report_agent_id(named) == 42

    assert gateway.requests == []


def test_a_report_with_no_agent_to_receive_it_fails_loudly(sdk_environment: MeivaContext) -> None:
    pages = (DirectoryPage("lead", None, (), None),)
    with (
        serve_gateway(pages) as gateway,
        gateway.client() as client,
        use_client(client),
        pytest.raises(RuntimeError, match="no report agent labelled 'lead' is available"),
    ):
        report_agent_id(_LEAD)


def test_a_notification_reaches_the_resolved_agent(sdk_environment: MeivaContext) -> None:
    pages = (DirectoryPage("lead", None, (AgentRowSpec(17, "lead", "idling"),), None),)
    with serve_gateway(pages) as gateway, gateway.client() as client, use_client(client):
        notify_report_agent(_LEAD, "collector failed")

    message = gateway.requests[-1]
    assert message.path == "/api/agents/17/messages"
    assert message.body["content"] == "collector failed"


def test_a_failed_notification_is_named_and_raised(
    sdk_environment: MeivaContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        serve_gateway(()) as gateway,
        gateway.client() as client,
        use_client(client),
        pytest.raises(Exception, match="404"),
    ):
        notify_report_agent(_LEAD, "collector failed")

    assert "probe-schedule could not notify the P0 lead" in capsys.readouterr().out


def test_every_agent_of_a_scope_is_read_across_pages(sdk_environment: MeivaContext) -> None:
    pages = (
        DirectoryPage("", None, (AgentRowSpec(300, "a"), AgentRowSpec(250, None)), 250),
        DirectoryPage("", 250, (AgentRowSpec(200, "b"),), None),
    )
    with serve_gateway(pages) as gateway, gateway.client() as client, use_client(client):
        ids = [row.agent_id for row in iter_agents(scope="all", limit=2)]

    assert ids == [300, 250, 200]
    assert gateway.directory_cursors() == [None, "250"]
    assert all(request.query["limit"] == "2" for request in gateway.requests)
