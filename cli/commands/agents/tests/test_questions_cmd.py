# meiva:note
# `meiva questions` — the operator CLI over the question queue (Task #949), against a gateway on a
# loopback port: the command dials it through its real transport and headers, and each case
# asserts what the gateway was asked and what the operator was shown.

from __future__ import annotations

import io

import httpx
import pytest
from pydantic import JsonValue

from base.clock.ids import new_uuid
from base.clock.tests.seeded_rng import seeded_rng
from cli.commands.agents.questions import (
    cmd_questions_answer,
    cmd_questions_list,
    cmd_questions_skip,
)
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving

OPEN = ("GET", "/api/questions/open")
OPEN_TARGET = "/api/questions/open"
NO_STDIN = io.StringIO()


def _question(nid: int, agent: int, *, title: str = "T") -> dict[str, JsonValue]:
    return {
        "id": nid,
        "agent_id": agent,
        "agent_label": "test",
        "title": title,
        "content": None,
    }


def _agent_route(agent: int) -> tuple[str, str]:
    return ("GET", f"/api/agents/{agent}")


def test_list_passes_include_awaiting() -> None:
    with serving({OPEN: Reply(body=[])}) as gateway:
        assert cmd_questions_list(access_to(gateway.url), agent_id=None) == 0
    assert [r.target for r in gateway.requests] == [OPEN_TARGET]
    assert gateway.requests[0].headers["authorization"] == "Bearer test-cluster-secret"


def test_list_prints_no_open_questions_for_an_empty_queue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({OPEN: Reply(body=[])}) as gateway:
        assert cmd_questions_list(access_to(gateway.url), agent_id=None) == 0
    assert capsys.readouterr().out == "No open questions.\n"


def test_list_filters_by_agent(capsys: pytest.CaptureFixture[str]) -> None:
    questions = [
        _question(1, 7, title="mine"),
        _question(3, 9, title="other agent"),
    ]
    with serving({OPEN: Reply(body=questions)}) as gateway:
        code = cmd_questions_list(access_to(gateway.url), agent_id=7)
    assert code == 0
    assert capsys.readouterr().out == "[1/1] #1 QUESTION agent#7 test: mine\n"


def _answers_route(agent: int) -> tuple[str, str]:
    return ("POST", f"/api/keyed/v1/agents/{agent}/questions/answers")


def _open_for(_agent: int) -> tuple[str, str]:
    return OPEN


def test_answer_posts_one_submission_covering_the_open_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        _open_for(7): Reply(body=[_question(3, 7), _question(5, 7)]),
        _answers_route(7): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_questions_answer(
            access_to(gateway.url),
            agent_id=7,
            answers=[(5, "2\nbecause")],
            skips=[3],
            idempotency_key="answer-key",
        )
    assert code == 0
    [read, post] = gateway.requests
    assert read.target == "/api/questions/open?agent_id=7"
    assert post.body == {
        "expected_ids": [3, 5],
        "results": [{"id": 5, "reply": "2\nbecause"}, {"id": 3, "skip": True}],
    }
    assert post.headers["idempotency-key"] == "answer-key"
    assert post.headers["idempotency-scope"] == "principal-v1"
    assert capsys.readouterr().out == "agent #7: settled 2 question(s)\n"


def test_answer_refuses_a_partial_cover_without_posting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        _open_for(7): Reply(body=[_question(3, 7), _question(5, 7)])
    }
    with serving(routes) as gateway:
        code = cmd_questions_answer(
            access_to(gateway.url),
            agent_id=7,
            answers=[(5, "yes")],
            skips=[],
            idempotency_key="answer-key",
        )
    assert code == 2
    assert "every open question must be answered or skipped once" in capsys.readouterr().out
    assert [r.method for r in gateway.requests] == ["GET"]


def test_answer_with_nothing_open_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({_open_for(7): Reply(body=[])}) as gateway:
        code = cmd_questions_answer(
            access_to(gateway.url), agent_id=7, answers=[], skips=[], idempotency_key="k"
        )
    assert code == 0
    assert capsys.readouterr().out == "agent #7: no open questions.\n"
    assert [r.method for r in gateway.requests] == ["GET"]


def test_answer_reports_a_changed_open_set_without_claiming_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        _open_for(7): Reply(body=[_question(3, 7)]),
        _answers_route(7): Reply(
            409, {"detail": "changed", "open": [_question(3, 7), _question(4, 7)]}
        ),
    }
    with serving(routes) as gateway:
        code = cmd_questions_answer(
            access_to(gateway.url), agent_id=7, answers=[(3, "x")], skips=[], idempotency_key="k"
        )
    assert code == 1
    assert "open now: [3, 4]" in capsys.readouterr().out
    assert len(gateway.requests) == 2


def test_answer_does_not_swallow_a_gateway_failure() -> None:
    routes: dict[tuple[str, str], Reply] = {
        _open_for(7): Reply(body=[_question(3, 7)]),
        _answers_route(7): Reply(422, {"detail": "bad"}),
    }
    with serving(routes) as gateway, pytest.raises(httpx.HTTPStatusError):
        cmd_questions_answer(
            access_to(gateway.url), agent_id=7, answers=[(3, "x")], skips=[], idempotency_key="k"
        )


def test_skip_submits_each_agents_whole_open_set() -> None:
    questions = [_question(1, 7, title="dec"), _question(2, 7), _question(3, 9)]
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=questions),
        _answers_route(7): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url), agent_id=7, force=True, stdin=NO_STDIN, rng=seeded_rng()
        )
    assert code == 0
    posts = [(r.target, r.body) for r in gateway.requests if r.method == "POST"]
    assert posts == [
        (
            "/api/keyed/v1/agents/7/questions/answers",
            {
                "expected_ids": [1, 2],
                "results": [{"id": 1, "skip": True}, {"id": 2, "skip": True}],
            },
        )
    ]
    assert [r.headers["idempotency-key"] for r in gateway.requests if r.method == "POST"] == [
        new_uuid(seeded_rng()).hex
    ]


def test_skip_reports_how_many_questions_the_gateway_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_question(1, 7), _question(2, 7)]),
        _answers_route(7): Reply(409, {"detail": "changed", "open": [_question(1, 7)]}),
    }
    with serving(routes) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url), agent_id=7, force=True, stdin=NO_STDIN, rng=seeded_rng()
        )
    assert code == 0
    assert capsys.readouterr().out.endswith("skipped 0/2 questions: agent #7\n")
    assert len(gateway.requests) == 2


@pytest.mark.parametrize("answer", ["y\n", "yes\n", "  Y  \n"])
def test_skip_proceeds_after_a_yes(answer: str, capsys: pytest.CaptureFixture[str]) -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_question(1, 7, title="dec")]),
        _answers_route(7): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url),
            agent_id=7,
            force=False,
            stdin=io.StringIO(answer),
            rng=seeded_rng(),
        )
    assert code == 0
    out = capsys.readouterr().out
    assert "Skip 1 question(s): agent #7?" in out
    assert "[1/1] #1 QUESTION agent#7 test: dec" in out
    assert [r.method for r in gateway.requests] == ["GET", "POST"]


@pytest.mark.parametrize("answer", ["", "n\n", "no\n", "maybe\n"])
def test_skip_aborts_unless_the_operator_says_yes(
    answer: str, capsys: pytest.CaptureFixture[str]
) -> None:
    routes: dict[tuple[str, str], Reply] = {OPEN: Reply(body=[_question(1, 7)])}
    with serving(routes) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url),
            agent_id=7,
            force=False,
            stdin=io.StringIO(answer),
            rng=seeded_rng(),
        )
    assert code == 1
    assert capsys.readouterr().out.endswith("aborted.\n")
    assert [r.method for r in gateway.requests] == ["GET"]


def test_skip_with_nothing_open_for_the_agent_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({OPEN: Reply(body=[_question(1, 9)])}) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url), agent_id=7, force=False, stdin=NO_STDIN, rng=seeded_rng()
        )
    assert code == 0
    assert capsys.readouterr().out == "agent #7: no open questions.\n"


def test_list_stale_filters_terminated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(
            body=[_question(1, 7, title="dead agent"), _question(2, 9, title="live agent")]
        ),
        _agent_route(7): Reply(body={"agent_id": 7, "status": "terminated"}),
        _agent_route(9): Reply(body={"agent_id": 9, "status": "running"}),
    }
    with serving(routes) as gateway:
        code = cmd_questions_list(access_to(gateway.url), agent_id=None, stale=True)
    assert code == 0
    assert [r.target for r in gateway.requests] == [OPEN_TARGET, "/api/agents/7", "/api/agents/9"]
    assert capsys.readouterr().out == "[1/1] #1 QUESTION agent#7 test [terminated]: dead agent\n"


def test_stale_lookup_deduplicates_only_agents_in_filtered_questions() -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_question(1, 7), _question(2, 7), _question(3, 9)]),
        _agent_route(7): Reply(body={"agent_id": 7, "status": "terminated"}),
    }
    with serving(routes) as gateway:
        code = cmd_questions_list(access_to(gateway.url), agent_id=7, stale=True)
    assert code == 0
    assert [r.target for r in gateway.requests] == [OPEN_TARGET, "/api/agents/7"]


def test_skip_stale_skips_only_terminated_agents() -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_question(1, 7, title="dead agent"), _question(2, 9)]),
        _agent_route(7): Reply(body={"agent_id": 7, "status": "terminated"}),
        _agent_route(9): Reply(body={"agent_id": 9, "status": "idling"}),
        _answers_route(7): Reply(201),
    }
    with serving(routes) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url),
            agent_id=None,
            force=True,
            stale=True,
            stdin=NO_STDIN,
            rng=seeded_rng(),
        )
    assert code == 0
    posts = [(r.target, r.body) for r in gateway.requests if r.method == "POST"]
    assert posts == [
        (
            "/api/keyed/v1/agents/7/questions/answers",
            {"expected_ids": [1], "results": [{"id": 1, "skip": True}]},
        )
    ]


def test_skip_stale_rejects_agent(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({}) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url),
            agent_id=7,
            force=True,
            stale=True,
            stdin=NO_STDIN,
            rng=seeded_rng(),
        )
    assert code == 2
    assert capsys.readouterr().out == "--stale and --agent are mutually exclusive\n"
    assert gateway.requests == []


def test_stale_empty_question_queue_never_reads_directory() -> None:
    with serving({OPEN: Reply(body=[])}) as gateway:
        access = access_to(gateway.url)
        assert cmd_questions_list(access, agent_id=None, stale=True) == 0
        assert (
            cmd_questions_skip(
                access, agent_id=None, force=True, stale=True, stdin=NO_STDIN, rng=seeded_rng()
            )
            == 0
        )
    assert [r.target for r in gateway.requests] == [OPEN_TARGET] * 2


def test_stale_skip_keeps_questions_of_missing_agents() -> None:
    routes: dict[tuple[str, str], Reply] = {
        OPEN: Reply(body=[_question(1, 7)]),
        _agent_route(7): Reply(404, {"detail": "no such agent"}),
    }
    with serving(routes) as gateway:
        code = cmd_questions_skip(
            access_to(gateway.url),
            agent_id=None,
            force=True,
            stale=True,
            stdin=NO_STDIN,
            rng=seeded_rng(),
        )
    assert code == 0
    assert [r.method for r in gateway.requests] == ["GET", "GET"]


def test_a_gateway_failure_reading_the_queue_is_not_swallowed() -> None:
    with (
        serving({OPEN: Reply(500, {"detail": "boom"})}) as gateway,
        pytest.raises(httpx.HTTPStatusError),
    ):
        cmd_questions_list(access_to(gateway.url), agent_id=None)


def test_list_cuts_a_long_question_body_to_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    decision: dict[str, JsonValue] = _question(4, 7, title="Pick one")
    decision["content"] = "x" * 200
    with serving({OPEN: Reply(body=[decision])}) as gateway:
        code = cmd_questions_list(access_to(gateway.url), agent_id=None)
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[1/1] #4 QUESTION agent#7 test: Pick one"
    assert out[1] == "    " + "x" * 120
