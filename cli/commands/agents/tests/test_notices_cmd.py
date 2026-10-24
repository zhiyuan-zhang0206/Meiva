# meiva:note
# `meiva notices` — the operator CLI over the notice queue (Task #949), against a gateway on a
# loopback port: the command dials it through its real transport and headers, and each case
# asserts what the gateway was asked and what the operator was shown.

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest

from cli.commands.agents.notices import cmd_notices_clear, cmd_notices_list, cmd_notices_resolve
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving

OPEN = ("GET", "/api/notices/open")
OPEN_TARGET = "/api/notices/open?include_awaiting=true"
NO_STDIN = io.StringIO()


def _notice(nid: int, agent: int, *, title: str = "T", require_response: bool = False) -> dict:
    return {
        "id": nid,
        "agent_id": agent,
        "agent_label": "test",
        "title": title,
        "content": None,
        "priority": "P2",
        "require_response": require_response,
        "blocking": False,
    }


def _resolve_route(agent: int, notice: int) -> tuple[str, str]:
    return ("POST", f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve")


def _agent_route(agent: int) -> tuple[str, str]:
    return ("GET", f"/api/agents/{agent}")


def test_list_passes_include_awaiting() -> None:
    with serving({OPEN: Reply(body=[])}) as gateway:
        assert (
            cmd_notices_list(access_to(gateway.url), agent_id=None, priority=None, type_filter=None)
            == 0
        )
    assert [r.target for r in gateway.requests] == [OPEN_TARGET]
    assert gateway.requests[0].headers["authorization"] == "Bearer test-cluster-secret"


def test_list_prints_no_open_notices_for_an_empty_queue(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({OPEN: Reply(body=[])}) as gateway:
        assert (
            cmd_notices_list(access_to(gateway.url), agent_id=None, priority=None, type_filter=None)
            == 0
        )
    assert capsys.readouterr().out == "No open notices.\n"


def test_list_filters_by_agent_and_type(capsys: pytest.CaptureFixture[str]) -> None:
    notices = [
        _notice(1, 7, title="dec", require_response=True),
        _notice(2, 7, title="fyi"),
        _notice(3, 9, title="other agent"),
    ]
    with serving({OPEN: Reply(body=notices)}) as gateway:
        code = cmd_notices_list(
            access_to(gateway.url), agent_id=7, priority=None, type_filter="fyi"
        )
    assert code == 0
    assert capsys.readouterr().out == "[1/1] #2 NOTICE P2 agent#7 test: fyi\n"


def test_resolve_requires_reply_for_answer(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({}) as gateway:
        code = cmd_notices_resolve(
            access_to(gateway.url), notice_id=1, agent_id=7, action="answer", reply=None
        )
    assert code == 2
    assert capsys.readouterr().out == "--reply TEXT is required for action=answer\n"
    assert gateway.requests == []


def test_resolve_refuses_an_unknown_action_without_dialing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({}) as gateway:
        code = cmd_notices_resolve(
            access_to(gateway.url), notice_id=1, agent_id=7, action="snooze", reply=None
        )
    assert code == 2
    assert "invalid action 'snooze'" in capsys.readouterr().out
    assert gateway.requests == []


def test_resolve_posts_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({_resolve_route(7, 5): Reply(201)}) as gateway:
        code = cmd_notices_resolve(
            access_to(gateway.url), notice_id=5, agent_id=7, action="dismiss", reply=None
        )
    assert code == 0
    [request] = gateway.requests
    assert request.target == "/api/keyed/v1/agents/7/notices/5/resolve"
    assert request.body == {"action": "dismiss", "reply": None}
    assert request.headers["idempotency-key"]
    assert request.headers["idempotency-scope"] == "principal-v1"
    assert capsys.readouterr().out == "notice #5 resolved: dismiss\n"


@pytest.mark.parametrize(
    ("action", "reply", "detail"),
    [
        ("dismiss", None, "notice 5 does not exist for agent 7"),
        ("dismiss", None, "legacy FYI entries are not notices"),
        ("dismiss", None, "notice 5 is already resolved for agent 7"),
        ("answer", "My answer", "notice 5 is already resolved for agent 7"),
    ],
)
def test_resolve_rejects_conflicts_without_claiming_success(
    action: str, reply: str | None, detail: str, capsys: pytest.CaptureFixture[str]
) -> None:
    conflict = Reply(409, {"detail": detail})
    with (
        serving({_resolve_route(7, 5): conflict}) as gateway,
        pytest.raises(httpx.HTTPStatusError) as error,
    ):
        cmd_notices_resolve(
            access_to(gateway.url), notice_id=5, agent_id=7, action=action, reply=reply
        )
    assert error.value.response.json() == {"detail": detail}
    assert len(gateway.requests) == 1
    assert capsys.readouterr().out == ""


def test_clear_resolves_each_open() -> None:
    notices = [_notice(1, 7, title="dec", require_response=True), _notice(2, 7), _notice(3, 9)]
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=notices),
        _resolve_route(7, 1): Reply(201),
        _resolve_route(7, 2): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_notices_clear(access_to(gateway.url), agent_id=7, force=True, stdin=NO_STDIN)
    assert code == 0
    posts = [(r.target, r.body) for r in gateway.requests if r.method == "POST"]
    assert posts == [
        ("/api/keyed/v1/agents/7/notices/1/resolve", {"action": "dismiss", "reply": None}),
        ("/api/keyed/v1/agents/7/notices/2/resolve", {"action": "dismiss", "reply": None}),
    ]


def test_clear_reports_how_many_notices_the_gateway_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_notice(1, 7), _notice(2, 7)]),
        _resolve_route(7, 1): Reply(201),
        _resolve_route(7, 2): Reply(409, {"detail": "already resolved"}),
    }
    with serving(routes) as gateway:
        code = cmd_notices_clear(access_to(gateway.url), agent_id=7, force=True, stdin=NO_STDIN)
    assert code == 0
    assert capsys.readouterr().out == "cleared 1/2 notices: agent #7\n"


@pytest.mark.parametrize("answer", ["y\n", "yes\n", "  Y  \n"])
def test_clear_resolves_after_a_yes(answer: str, capsys: pytest.CaptureFixture[str]) -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_notice(1, 7, title="dec")]),
        _resolve_route(7, 1): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_notices_clear(
            access_to(gateway.url), agent_id=7, force=False, stdin=io.StringIO(answer)
        )
    assert code == 0
    out = capsys.readouterr().out
    assert "Clear 1 notice(s): agent #7?" in out
    assert "[1/1] #1 NOTICE P2 agent#7 test: dec" in out
    assert [r.method for r in gateway.requests] == ["GET", "POST"]


@pytest.mark.parametrize("answer", ["", "n\n", "no\n", "maybe\n"])
def test_clear_aborts_unless_the_operator_says_yes(
    answer: str, capsys: pytest.CaptureFixture[str]
) -> None:
    routes: dict[tuple[str, str], Reply] = {OPEN: Reply(body=[_notice(1, 7)])}
    with serving(routes) as gateway:
        code = cmd_notices_clear(
            access_to(gateway.url), agent_id=7, force=False, stdin=io.StringIO(answer)
        )
    assert code == 1
    assert capsys.readouterr().out.endswith("aborted.\n")
    assert [r.method for r in gateway.requests] == ["GET"]


def test_clear_with_nothing_open_for_the_agent_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({OPEN: Reply(body=[_notice(1, 9)])}) as gateway:
        code = cmd_notices_clear(access_to(gateway.url), agent_id=7, force=False, stdin=NO_STDIN)
    assert code == 0
    assert capsys.readouterr().out == "agent #7: no open notices.\n"


def test_list_stale_filters_terminated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_notice(1, 7, title="dead agent"), _notice(2, 9, title="live agent")]),
        _agent_route(7): Reply(body={"agent_id": 7, "status": "terminated"}),
        _agent_route(9): Reply(body={"agent_id": 9, "status": "running"}),
    }
    with serving(routes) as gateway:
        code = cmd_notices_list(
            access_to(gateway.url), agent_id=None, priority=None, type_filter=None, stale=True
        )
    assert code == 0
    assert [r.target for r in gateway.requests] == [OPEN_TARGET, "/api/agents/7", "/api/agents/9"]
    assert capsys.readouterr().out == "[1/1] #1 NOTICE P2 agent#7 test [terminated]: dead agent\n"


def test_stale_lookup_deduplicates_only_agents_in_filtered_notices() -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_notice(1, 7), _notice(2, 7), _notice(3, 9, require_response=True)]),
        _agent_route(7): Reply(body={"agent_id": 7, "status": "terminated"}),
    }
    with serving(routes) as gateway:
        code = cmd_notices_list(
            access_to(gateway.url), agent_id=None, priority=None, type_filter="fyi", stale=True
        )
    assert code == 0
    assert [r.target for r in gateway.requests] == [OPEN_TARGET, "/api/agents/7"]


def test_clear_stale_resolves_only_terminated() -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_notice(1, 7, title="dead agent", require_response=True), _notice(2, 9)]),
        _agent_route(7): Reply(body={"agent_id": 7, "status": "terminated"}),
        _agent_route(9): Reply(body={"agent_id": 9, "status": "idling"}),
        _resolve_route(7, 1): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_notices_clear(
            access_to(gateway.url), agent_id=None, force=True, stale=True, stdin=NO_STDIN
        )
    assert code == 0
    posts = [(r.target, r.body) for r in gateway.requests if r.method == "POST"]
    assert posts == [
        ("/api/keyed/v1/agents/7/notices/1/resolve", {"action": "dismiss", "reply": None})
    ]


def test_clear_stale_rejects_agent(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({}) as gateway:
        code = cmd_notices_clear(
            access_to(gateway.url), agent_id=7, force=True, stale=True, stdin=NO_STDIN
        )
    assert code == 2
    assert capsys.readouterr().out == "--stale and --agent are mutually exclusive\n"
    assert gateway.requests == []


def test_stale_empty_notice_queue_never_reads_directory() -> None:
    with serving({OPEN: Reply(body=[])}) as gateway:
        access = access_to(gateway.url)
        assert (
            cmd_notices_list(access, agent_id=None, priority=None, type_filter=None, stale=True)
            == 0
        )
        assert cmd_notices_clear(access, agent_id=None, force=True, stale=True, stdin=NO_STDIN) == 0
    assert [r.target for r in gateway.requests] == [OPEN_TARGET] * 2


def test_stale_clear_keeps_notices_of_missing_agents() -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_notice(1, 7)]),
        _agent_route(7): Reply(404, {"detail": "no such agent"}),
    }
    with serving(routes) as gateway:
        code = cmd_notices_clear(
            access_to(gateway.url), agent_id=None, force=True, stale=True, stdin=NO_STDIN
        )
    assert code == 0
    assert [r.method for r in gateway.requests] == ["GET", "GET"]


def test_a_gateway_failure_reading_the_queue_is_not_swallowed() -> None:
    with (
        serving({OPEN: Reply(500, {"detail": "boom"})}) as gateway,
        pytest.raises(httpx.HTTPStatusError),
    ):
        cmd_notices_list(access_to(gateway.url), agent_id=None, priority=None, type_filter=None)


def test_list_cuts_a_long_notice_body_to_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    decision: dict[str, Any] = _notice(4, 7, title="Pick one", require_response=True)
    decision["content"] = "x" * 200
    with serving({OPEN: Reply(body=[decision])}) as gateway:
        code = cmd_notices_list(
            access_to(gateway.url), agent_id=None, priority=None, type_filter="decision"
        )
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[1/1] #4 NOTICE P2 agent#7 test: Pick one"
    assert out[1] == "    " + "x" * 120
