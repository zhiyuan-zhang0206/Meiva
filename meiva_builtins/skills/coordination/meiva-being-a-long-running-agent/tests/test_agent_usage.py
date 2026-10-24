"""Real SQL coverage for selectable usage scopes and notification-only budgets."""

import importlib
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import Database
from base.telemetry.metrics import usage as reader
from meiva.tests.fixtures.gateway_wire import GatewayWire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire
from meiva.tests.fixtures.pin_agent import pin_agent

_SCRIPTS = "meiva_builtins.skills.coordination.meiva-being-a-long-running-agent.scripts"
usage = importlib.import_module(f"{_SCRIPTS}.agent_usage")
budget = importlib.import_module(f"{_SCRIPTS}.usage_budget")
_CALLER = 42


# meiva:note
# Pin the calling agent on the wire and let each peer accept a message.
def _peer(wire: GatewayWire, *peers: int) -> None:
    pin_agent(_CALLER, clients=wire.context.clients)
    for peer in peers:
        wire.stub.reply("POST", f"/api/agents/{peer}/messages", {"status": "enqueued"}, status=201)


def _messages(wire: GatewayWire) -> list[tuple[str, str, Any]]:
    return [(sent.method, sent.path, sent.body) for sent in wire.sent]


def seed_agent(conn: psycopg.Connection, parent: int | None = None, *, fork: bool = False) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    spawner = "user" if parent is None else f"agent:{parent}"
    conn.execute(
        "INSERT INTO agents_meta (id, status, spawner, born_spawner, fork_source_agent_id, fork_source_checkpoint_id) VALUES (%s,'idling',%s,%s,%s,%s)",
        (agent_id, spawner, spawner, parent if fork else None, "test-checkpoint" if fork else None),
    )
    return agent_id


def event(
    conn: psycopg.Connection, agent_id: int, ts: datetime, attributes: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO telemetry_events (event_uid,ts,agent_id,machine,cluster,process,category,event_name,level,source,attributes) "
        "VALUES (%s,%s,%s,'test','test','test','telemetry','llm_usage','info','test',%s)",
        (agent_id * 1_000_000 + int(ts.timestamp()), ts, agent_id, Jsonb(attributes)),
    )


def test_birth_lineage_modes_overlap_and_folding(db_conn: psycopg.Connection) -> None:
    root = seed_agent(db_conn)
    child = seed_agent(db_conn, root)
    fork = seed_agent(db_conn, root, fork=True)
    mixed = seed_agent(db_conn, child, fork=True)
    grandchild = seed_agent(db_conn, child)
    fork_child = seed_agent(db_conn, fork)
    other = seed_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET spawner = 'user' WHERE id = %s", (child,))
    assert reader.select_agents(db_conn, [root], "self") == [root]
    assert reader.select_agents(db_conn, [root], "spawn") == sorted([root, child, grandchild])
    assert reader.select_agents(db_conn, [root], "fork") == sorted([root, fork])
    assert reader.select_agents(db_conn, [root, child], "all") == sorted(
        [root, child, fork, mixed, grandchild, fork_child]
    )
    assert other not in reader.select_agents(db_conn, [root], "all")
    with pytest.raises(ValueError, match="unknown agent IDs"):
        reader.select_agents(db_conn, [999_999_999], "all")
    with pytest.raises(ValueError, match="unknown lineage"):
        reader.select_agents(db_conn, [root], "unknown")  # pyright: ignore[reportArgumentType]


def test_window_prices_zero_usage_and_unpriced_events(db_conn: psycopg.Connection) -> None:
    root = seed_agent(db_conn)
    child = seed_agent(db_conn, root)
    idle = seed_agent(db_conn, root)
    other = seed_agent(db_conn)
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    end = start + timedelta(minutes=4)
    priced = {"in_total": 100, "out_total": 20, "cache_read": 60, "cost_usd": 0.25}
    event(db_conn, root, start - timedelta(seconds=1), priced)  # outside lower boundary
    event(db_conn, root, start, priced)
    event(
        db_conn,
        child,
        end - timedelta(seconds=1),
        {"in_total": 30, "out_total": 5, "cache_read": 0, "unpriced": 1},
    )
    event(db_conn, child, end + timedelta(seconds=1), priced)
    event(db_conn, other, end, priced)
    report = reader.usage_report(db_conn, roots=[root, child], lineage="all", start=start, end=end)
    assert report["totals"] == {
        "calls": 2,
        "input_tokens": 130,
        "output_tokens": 25,
        "cache_read_tokens": 60,
        "reasoning_tokens": 0,
        "total_tokens": 155,
        "recorded_cost_usd": 0.25,
        "unpriced_calls": 1,
    }
    assert next(row for row in report["agents"] if row["agent_id"] == idle)["calls"] == 0
    with pytest.raises(ValueError, match="start < end"):
        reader.usage_report(db_conn, roots=[root], lineage="self", start=end, end=start)


_FACTS = budget.BudgetFacts(
    recorded_cost_usd=0.25, unpriced_calls=1, agent_ids=[1], lineage="self", start="s", end="e"
)


def test_budget_reminder_states_the_cost_against_the_limit_and_terminates_nobody() -> None:
    assert budget.budget_message(_FACTS, usd_limit=1.0) is None
    assert budget.budget_message(_FACTS, usd_limit=None) is None
    message = budget.budget_message(_FACTS, usd_limit=0.25)
    assert "handoff" in message and "Unpriced calls: 1" in message
    assert "recorded USD 0.250000 >= 0.25" in message
    assert "has not terminated any agent" in message
    for limit in (float("nan"), float("inf"), -1, 0, True):
        with pytest.raises(ValueError, match="positive and finite"):
            budget.budget_message(_FACTS, usd_limit=limit)
    with pytest.raises(ValueError, match="timezone"):
        budget.parse_time("2026-10-06T12:00:00")
    with pytest.raises(ValueError, match="positive integers"):
        budget.positive_id("0")


# meiva:note
# The reminder is a function of recorded cost only: a billion tokens at a quarter dollar stays quiet.
def test_token_counts_are_never_a_budget_threshold() -> None:
    report: dict[str, Any] = {
        "totals": {"total_tokens": 1_000_000_000, "recorded_cost_usd": 0.25, "unpriced_calls": 0},
        "agents": [{"agent_id": 1}],
        "lineage": "self",
        "start": "start",
        "end": "end",
    }

    facts = usage.facts_of(report)

    assert facts.agent_ids == [1]
    assert budget.budget_message(facts, usd_limit=1.0) is None


def test_next_step_reminds_once_then_polls_or_finishes() -> None:
    assert budget.next_step("over budget", 30.0) == budget.Notify("over budget")
    assert budget.next_step("over budget", None) == budget.Notify("over budget")
    assert budget.next_step(None, 30.0) == budget.Sleep(30.0)
    assert budget.next_step(None, None) == budget.Finish()


def test_budget_reminds_named_peers_without_lifecycle_actions(wire: GatewayWire) -> None:
    _peer(wire, 10, 20)

    usage.notify_agents([20, 10, 20], "over budget")

    assert _messages(wire) == [
        (
            "POST",
            "/api/agents/10/messages",
            {"content": "over budget", "source": f"agent:{_CALLER}"},
        ),
        (
            "POST",
            "/api/agents/20/messages",
            {"content": "over budget", "source": f"agent:{_CALLER}"},
        ),
    ]


@pytest.mark.parametrize(
    "options",
    [
        ["--lineage", "unknown"],
        ["--end", "2026-10-06T00:00:00+00:00"],
        ["--poll-seconds", "30"],
        ["--notify-agent", "2"],
        ["--poll-seconds", "nan", "--usd-limit", "1", "--notify-agent", "2"],
        ["--token-limit", "100"],
    ],
)
def test_cli_rejects_invalid_observation_modes(options: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        usage.parse_args(["--agent-id", "1", "--lifetime", *options])
    assert exc.value.code == 2


def test_cli_reads_a_real_window_and_sends_one_reminder(
    db_conn: psycopg.Connection,
    database: Database,
    wire: GatewayWire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = seed_agent(db_conn)
    start = datetime.now(UTC) - timedelta(minutes=1)
    event(
        db_conn,
        root,
        start + timedelta(seconds=1),
        {"in_total": 100, "out_total": 20, "cost_usd": 0.1},
    )
    db_conn.commit()
    _peer(wire, root)

    usage.main(
        [
            "--agent-id",
            str(root),
            "--start",
            start.isoformat(),
            "--usd-limit",
            "0.1",
            "--notify-agent",
            str(root),
        ],
        database=database,
    )

    result = json.loads(capsys.readouterr().out)
    assert result["totals"]["total_tokens"] == 120
    [(method, path, body)] = _messages(wire)
    assert (method, path) == ("POST", f"/api/agents/{root}/messages")
    assert "handoff" in body["content"]


def test_a_single_read_below_the_limit_prints_the_report_and_notifies_nobody(
    db_conn: psycopg.Connection,
    database: Database,
    wire: GatewayWire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = seed_agent(db_conn)
    start = datetime.now(UTC) - timedelta(minutes=1)
    event(db_conn, root, start + timedelta(seconds=1), {"in_total": 1, "cost_usd": 0.01})
    db_conn.commit()
    _peer(wire, root)

    usage.main(
        ["--agent-id", str(root), "--start", start.isoformat(), "--usd-limit", "1"],
        database=database,
    )

    assert len(capsys.readouterr().out.splitlines()) == 1
    assert wire.sent == []


def test_polling_reads_each_round_until_the_budget_is_breached(
    db_conn: psycopg.Connection,
    db_url: str,
    database: Database,
    wire: GatewayWire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = seed_agent(db_conn)
    start = datetime.now(UTC) - timedelta(minutes=1)
    event(db_conn, root, start + timedelta(seconds=1), {"in_total": 1, "cost_usd": 0.05})
    db_conn.commit()
    _peer(wire, 2)

    def breach() -> None:
        with psycopg.connect(db_url) as conn:
            event(conn, root, datetime.now(UTC), {"in_total": 1, "cost_usd": 0.1})

    timer = threading.Timer(0.3, breach)
    timer.start()
    try:
        usage.main(
            [
                "--agent-id",
                str(root),
                "--start",
                start.isoformat(),
                "--usd-limit",
                "0.1",
                "--notify-agent",
                "2",
                "--poll-seconds",
                "0.02",
            ],
            database=database,
        )
    finally:
        timer.cancel()
        timer.join()

    costs = [
        json.loads(line)["totals"]["recorded_cost_usd"]
        for line in capsys.readouterr().out.splitlines()
    ]
    assert costs[0] == 0.05
    assert len(costs) > 1
    assert costs[-1] == pytest.approx(0.15)
    [(method, path, _body)] = _messages(wire)
    assert (method, path) == ("POST", "/api/agents/2/messages")


def test_the_process_root_builds_one_database_handle_for_every_poll(db_url: str) -> None:
    database = usage.process_database()

    with database.connect() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_lifetime_report_combines_ledger_and_tail(db_conn: psycopg.Connection) -> None:
    root = seed_agent(db_conn)
    now = datetime.now(UTC)
    yesterday = now.date() - timedelta(days=1)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id,day,model,llm_calls,costed_calls,unpriced_calls,tokens_in,tokens_out,cost_usd) "
        "VALUES (%s,%s,'m',2,2,0,100,20,0.5)",
        (root, yesterday),
    )
    event(
        db_conn,
        root,
        now - timedelta(seconds=1),
        {"model": "m", "in_total": 10, "out_total": 2, "cost_usd": 0.1},
    )
    report = reader.usage_report(db_conn, roots=[root], lineage="self", start=None, end=now)
    assert report["totals"]["calls"] == 3
    assert report["totals"]["total_tokens"] == 132
    assert report["totals"]["recorded_cost_usd"] == 0.6
    assert report["agents"][0]["by_model"]["m"]["llm_calls"] == 3
