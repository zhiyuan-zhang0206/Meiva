# meiva:note
# Window-scoped inbound collection for the self-evolution daily scan.
#
# Regression lock for 2026-08-13: ``_inbounds_by_agent`` fetched every inbound message an agent ever received (no time filter), so ``corrections`` / ``peer_feedback`` accumulated lifetime history into each day's record — the whole fleet was flagged fumbled every day (1336 "peer feedback" messages across 70 runs on a day when agents were fine). It now fetches the collection window plus each agent's first-ever chat message (the task prompt, which may predate the window for long-running agents).
#
# The events side of the collection runs against a real listener on port 0 (`collection_world.serve_events`), the database side against the throwaway Postgres, and the checkpoint reads against checkpoints written to it.

import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.types.json import Jsonb

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import GatewayApiBaseMissing
from base.config import settings
from base.db import Database
from base.host.net.resilience import retry_sleep
from base.host.net.tests.local_http import closed_port_url
from base.host.proc import run_bounded
from meiva_builtins.skills.tests.skill_script import load_skill_script

world = load_skill_script("platform", "meiva-self-evolution", "tests", "collection_world.py")
record_mod = load_skill_script("platform", "meiva-self-evolution", "scripts", "record.py")
window_mod = load_skill_script("platform", "meiva-self-evolution", "scripts", "events_window.py")
_REPO = Path(__file__).parents[5]


@pytest.fixture(scope="module")
def collect_mod() -> Any:
    """The collection logic module, loaded by path (kebab-case skill dirs never import)."""
    return load_skill_script("platform", "meiva-self-evolution", "scripts", "collect.py")


def _fetch(collect_mod: Any, cur: psycopg.Cursor, agent_ids: list[int]) -> Any:
    now = datetime.now(UTC)
    return collect_mod._inbounds_by_agent(cur, agent_ids, now - timedelta(days=1), now)


# meiva:note
# A record built from the empty case of every input, overridden by `fields`.
def _record(**fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "agent_id": 1,
        "week": "2026-W99",
        "events": (),
        "log_events": (),
        "inbounds": (),
        "meta": record_mod.AgentMeta("user", "completed", "done"),
        "transcript": (),
        "skills_touched": (),
    }
    values.update(fields)
    values["events"] = record_mod.event_facts(values["events"])
    values["log_events"] = tuple(name for name, _ in values["log_events"])
    values["inbounds"] = record_mod.inbound_facts(values["inbounds"])
    values["transcript"] = record_mod.transcript_facts(values["transcript"])
    return cast(dict[str, Any], record_mod.build_record(record_mod.RunInputs(**values)))


def test_old_history_collapses_to_task_prompt(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    """Only the first-ever message survives from outside the window — not the
    whole lifetime of corrections / peer pings."""
    with db_conn.cursor() as cur:
        world.insert_agent(cur, 1)
        world.insert_agent(cur, 2)
        world.insert_inbound(cur, 1, "user", "task prompt", "10 days")
        world.insert_inbound(cur, 1, "user", "old correction \u9519\u4e86", "8 days")
        world.insert_inbound(cur, 1, "user", "window follow-up", "1 hour")
        world.insert_inbound(cur, 2, "user", "task prompt 2", "30 days")
        world.insert_inbound(cur, 2, "agent:99", "old peer ping \u522b\u5fd8\u4e86", "29 days")
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [1, 2])

    assert [m["content"] for m in out[1]] == ["task prompt", "window follow-up"]
    assert [m["content"] for m in out[2]] == ["task prompt 2"]


def test_fresh_worker_uses_in_window_spawner_prompt(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    """A worker's recent spawn brief is its only task prompt."""
    with db_conn.cursor() as cur:
        world.insert_agent(cur, 9)
        world.insert_inbound(cur, 9, "agent:1", "fresh spawn brief", "2 hours")
    db_conn.commit()

    with db_conn.cursor() as cur:
        inbounds = _fetch(collect_mod, cur, [9])

    rec = _record(
        agent_id=9,
        week="2026-08-30",
        inbounds=tuple(inbounds[9]),
        meta=record_mod.AgentMeta("agent:1", "completed", "delivered"),
    )

    assert rec["task_prompt"] == "fresh spawn brief"


def test_first_ever_inside_window_not_duplicated(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        world.insert_agent(cur, 3)
        world.insert_inbound(cur, 3, "user", "spawn prompt", "2 hours")
        world.insert_inbound(cur, 3, "user", "follow-up", "1 hour")
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [3])

    assert [m["content"] for m in out[3]] == ["spawn prompt", "follow-up"]


def test_inbounds_mark_same_instruction_sent_to_multiple_agents_as_broadcast(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    """Fan-out instructions are fleet-scoped, not per-agent corrections."""
    with db_conn.cursor() as cur:
        for agent_id in (5, 6):
            world.insert_agent(cur, agent_id)
            world.insert_inbound(cur, agent_id, "user", f"task prompt {agent_id}", "2 days")
            world.insert_inbound(
                cur,
                agent_id,
                "user",
                "wrong: pause all work until resumed",
                "1 hour",
            )
        world.insert_inbound(cur, 5, "user", "wrong: use the requested path", "30 minutes")
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [5, 6])

    agent_five = {m["content"]: m["is_broadcast"] for m in out[5]}
    assert agent_five["task prompt 5"] is False
    assert agent_five["wrong: pause all work until resumed"] is True
    assert agent_five["wrong: use the requested path"] is False


def test_agent_without_inbounds_absent(collect_mod: Any, db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        world.insert_agent(cur, 4)
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [4])

    assert out.get(4, []) == []


def _put_checkpoint(thread_id: str, messages: list[Any]) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=cast(CheckpointMetadata, {"source": "input", "step": 1, "parents": {}}),
            new_versions={"messages": "1"},
        )


def test_transcript_uses_full_checkpoint_loader(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    _put_checkpoint("42", [HumanMessage(content="complete history"), AIMessage(content="done")])

    assert collect_mod._transcript(42, database=database) == [
        {"type": "human", "content": "complete history"},
        {"type": "ai", "content": "done"},
    ]
    assert collect_mod._transcript(43, database=database) == []


# meiva:note
# A checkpoint that cannot be deserialized must not fail the whole scan, but it must not pass silently either: the warning names the agent.
def test_unreadable_checkpoint_leaves_an_empty_transcript_and_says_so(
    collect_mod: Any,
    db_conn: psycopg.Connection,
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _put_checkpoint("44", [HumanMessage(content="unreadable")])
    with db_conn.cursor() as cur:
        cur.execute("UPDATE checkpoint_blobs SET blob = '\\x00' WHERE thread_id = '44'")
    db_conn.commit()

    assert collect_mod._transcript(44, database=database) == []
    assert "warning: agent 44: checkpoint unreadable" in capsys.readouterr().out


def test_subprocess_call_count_counts_raw_calls_and_textual_mentions() -> None:
    """The metric intentionally regex-scans code bodies rather than parsing Python."""
    window_records = load_skill_script(
        "platform", "meiva-self-evolution", "scripts", "window_records.py"
    )
    events = [
        (
            "code",
            {
                "body": '''
subprocess.run(["a"])
subprocess.Popen(["b"])
subprocess.check_output(["c"])
subprocess.check_call(["d"])
subprocess.call(["e"])
# subprocess.run(["comment"])
"""subprocess.Popen(["docstring"])"""
example = "subprocess.check_output([\\"literal\\"])"
asyncio.create_subprocess_exec("not-counted")
subprocess.getoutput("not-counted")
'''
            },
        ),
        ("turn_end", {"body": 'subprocess.call(["not-code"])'}),
    ]

    assert window_records.subprocess_call_count(record_mod.event_facts(events)) == 8


def _read(
    collect_mod: Any,
    server: Any,
    *,
    category: str = "telemetry",
    seconds: float = 7200,
    agent_id: int | None = None,
    gateway: Any = None,
    sleep: Callable[[float], None] = retry_sleep,
) -> list[dict[str, Any]]:
    window = window_mod.Window(world.ts_after(0), world.ts_after(seconds))
    return cast(
        list[dict[str, Any]],
        collect_mod.fetch_events_window(
            category,
            window,
            gateway=gateway if gateway is not None else world.grant(server.url),
            agent_id=agent_id,
            sleep=sleep,
        ),
    )


def test_fetch_slices_and_orders_oldest_first(collect_mod: Any) -> None:
    # 2500 rows > single-page budget -> binary-search time; one offset=0 query per slice
    rows = [world.event_row(i, world.ts_after(i * 2)) for i in range(2500)]
    with world.serve_events(rows) as server:
        out = _read(collect_mod, server)

    assert len(out) == 2500
    tss = [str(r["ts"]) for r in out]
    assert tss == sorted(tss)
    # No offset pagination: every request is offset=0 limit=1000 (Loki offset pagination times out / drops data)
    assert server.requests
    for request in server.requests:
        assert request.params["offset"] == "0"
        assert request.params["limit"] == "1000"


def test_fetch_bisects_oversized_slices_without_loss(collect_mod: Any) -> None:
    # 9000 rows across a 5-hour window -> over the 4000-row budget -> must split via binary search
    rows = [world.event_row(i, world.ts_after(i * 2)) for i in range(9000)]
    with world.serve_events(rows) as server:
        out = _read(collect_mod, server, seconds=18000)

    assert len(out) == 9000
    assert len({r["id"] for r in out}) == 9000
    for request in server.requests:
        assert request.params["offset"] == "0"
        assert request.params["limit"] == "1000"


def test_fetch_dedups_inclusive_slice_boundaries(collect_mod: Any) -> None:
    # Boundary rows (exactly at the bisection midpoint) are included by both adjacent slices (window includes both ends) -> dedupe by id
    boundary = world.event_row(999, world.ts_after(1800))
    rows = [world.event_row(i, world.ts_after(i * 2)) for i in range(2500)] + [boundary]
    with world.serve_events(rows) as server:
        out = _read(collect_mod, server, seconds=3600)

    ids = [r["id"] for r in out]
    assert len(ids) == len(set(ids))
    assert ids.count(999) == 1


def test_fetch_passes_agent_filter_and_bearer_auth(collect_mod: Any) -> None:
    with world.serve_events([]) as server:
        _read(collect_mod, server, category="audit", seconds=3600, agent_id=42)

    assert server.requests
    for request in server.requests:
        assert request.params["agent_id"] == "42"
        assert request.params["category"] == "audit"
        assert request.headers["authorization"] == "Bearer test-secret"


def test_fetch_raises_on_http_error(collect_mod: Any) -> None:
    with (
        world.serve_events([], script=[world.Scripted(500)]) as server,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _read(collect_mod, server, seconds=3600)


def test_fetch_requests_are_count_free(collect_mod: Any) -> None:
    """2026-08-18 contract lock: /api/events meta.total is opt-in behind
    with_total=1 because the count aggregation is exactly the Loki load the
    gateway shed. Paging decisions use has_more; no request may ask for the
    count (a re-introduced with_total=1 would silently re-add that load)."""
    rows = [world.event_row(i, world.ts_after(i * 2)) for i in range(2500)]
    with world.serve_events(rows) as server:
        _read(collect_mod, server)

    assert server.requests
    for request in server.requests:
        assert "with_total" not in request.params


# meiva:note
# The gateway URL is a required value: an unset one fails before any request instead of falling back to a guessed local address.
def test_fetch_requires_a_gateway_url(collect_mod: Any) -> None:
    window = window_mod.Window(world.ts_after(0), world.ts_after(3600))
    with pytest.raises(GatewayApiBaseMissing):
        collect_mod.fetch_events_window("audit", window, gateway=world.grant(""))


# meiva:note
# 2026-09-12 regression: httpx honored the env proxy for cluster-internal reads (a no_proxy form it does not match), so /api/events went to a local forward proxy and came back 502 while the gateway was healthy. The client must ignore the proxy variables, like ops/cluster/rpc.py. The child gets only the environment written here: a proxy that refuses connections, and nothing that excuses 127.0.0.1 from it.
def test_fetch_client_disables_env_proxy(tmp_path: Path) -> None:
    rows = [world.event_row(1, world.ts_after(10), category="audit")]
    with world.serve_events(rows) as server:
        dead_proxy = closed_port_url()
        completed = run_bounded(
            [
                sys.executable,
                "meiva_builtins/skills/platform/meiva-self-evolution/tests/events_proxy_child.py",
                server.url,
            ],
            cwd=_REPO,
            env={
                "HTTP_PROXY": dead_proxy,
                "http_proxy": dead_proxy,
                "PATH": "/usr/bin:/bin",
                "MEIVA_HOME": str(tmp_path / "home"),
                "PYTHONPATH": str(_REPO),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "1"
    assert len(server.requests) == 1


# ── retry budget + failure diagnostics (Task #3126) ────────────────────────


def test_fetch_retries_transient_then_succeeds(collect_mod: Any) -> None:
    """A blip inside the budget must not cost the day's dataset."""
    timers = ManualTimers()
    rows = [world.event_row(1, world.ts_after(0))]
    script = [world.Scripted(502), world.Scripted(None)]
    with world.serve_events(rows, script=script) as server:
        out = _read(collect_mod, server, seconds=3600, sleep=timers.sleep)

    assert [r["id"] for r in out] == [1]
    assert len(server.requests) == 3
    assert timers.sleeps == [5.0, 10.0]


@pytest.mark.parametrize("code", [502, 503, 504])
def test_fetch_retryable_status_exhausts_budget_with_diagnostics(
    collect_mod: Any, code: int
) -> None:
    """2026-09-12 regression: a spent budget must raise with the responder's
    status, headers, and body — the evidence that tells a local forward
    proxy's 502 from the gateway's own 503 — on the 5/10/20/40 s schedule."""
    timers = ManualTimers()
    reply = world.Scripted(
        code,
        headers={"content-type": "text/html", "server": "forward-proxy"},
        body="<html>Bad Gateway</html>",
    )
    with world.serve_events([], script=[reply] * 5) as server, pytest.raises(RuntimeError) as err:
        _read(collect_mod, server, seconds=3600, sleep=timers.sleep)

    message = str(err.value)
    assert f"HTTP {code}" in message
    assert "server" in message and "forward-proxy" in message
    assert "<html>Bad Gateway</html>" in message
    assert "after 5 attempts" in message
    assert len(server.requests) == 5
    assert timers.sleeps == [5.0, 10.0, 20.0, 40.0]
    assert isinstance(err.value.__cause__, httpx.HTTPStatusError)


# meiva:note
# Transport errors (httpx.TransportError includes TimeoutException) run the same budget, and the spent-budget error names the failure.
def test_fetch_connection_refused_exhausts_budget_with_diagnostics(collect_mod: Any) -> None:
    timers = ManualTimers()
    window = window_mod.Window(world.ts_after(0), world.ts_after(3600))
    with pytest.raises(RuntimeError) as err:
        collect_mod.fetch_events_window(
            "telemetry", window, gateway=world.grant(closed_port_url()), sleep=timers.sleep
        )

    cause = err.value.__cause__
    assert isinstance(cause, httpx.ConnectError)
    assert f"{type(cause).__name__}: {cause}" in str(err.value)
    assert timers.sleeps == [5.0, 10.0, 20.0, 40.0]


def test_fetch_dropped_connection_exhausts_budget_with_diagnostics(collect_mod: Any) -> None:
    timers = ManualTimers()
    with (
        world.serve_events([], script=[world.Scripted(None)] * 5) as server,
        pytest.raises(RuntimeError) as err,
    ):
        _read(collect_mod, server, seconds=3600, sleep=timers.sleep)

    cause = err.value.__cause__
    assert isinstance(cause, httpx.TransportError)
    assert f"{type(cause).__name__}: {cause}" in str(err.value)
    assert len(server.requests) == 5
    assert timers.sleeps == [5.0, 10.0, 20.0, 40.0]


def test_fetch_exhausted_diagnostics_are_bounded(collect_mod: Any) -> None:
    """A huge body / header block must not flood the scan's log line: each
    diagnostic field is capped, with an explicit truncation marker."""
    timers = ManualTimers()
    reply = world.Scripted(503, headers={"x-note": "h" * 1000}, body="b" * 1000)
    with world.serve_events([], script=[reply] * 5) as server, pytest.raises(RuntimeError) as err:
        _read(collect_mod, server, seconds=3600, sleep=timers.sleep)

    message = str(err.value)
    assert "b" * 300 in message and "(+700 chars)" in message  # body capped at 300
    assert message.count("(+") == 2  # the header block is capped too
    assert len(message) < 800


@pytest.mark.parametrize("code", [429, 500, 404])
def test_fetch_permanent_status_fails_fast(collect_mod: Any, code: int) -> None:
    """Task #3126: only 502/503/504 are retryable. 429/500 (transient in the
    shared default) and every other status are structural for this read —
    one attempt, no sleep, the original HTTPStatusError."""
    timers = ManualTimers()
    with (
        world.serve_events([], script=[world.Scripted(code)] * 5) as server,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _read(collect_mod, server, seconds=3600, sleep=timers.sleep)

    assert len(server.requests) == 1
    assert timers.sleeps == []


# meiva:note
# The single-agent path builds the record from the gateway's events and the agent's database rows, and a malformed `plugin_activation` row is counted on the record and named in a warning (issue #92).
def test_collect_one_reads_one_agents_week_and_warns_about_malformed_plugin_rows(
    collect_mod: Any,
    db_conn: psycopg.Connection,
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = world.ts_after(3600)
    rows = [
        world.event_row(
            1,
            world.ts_after(10),
            event_name="llm_usage",
            attributes={"model": "m", "in_total": 100, "out_total": 20},
        ),
        world.event_row(
            2, world.ts_after(20), event_name="turn_end", attributes={"duration_seconds": 3}
        ),
        world.event_row(
            3,
            world.ts_after(30),
            event_name="plugin_activation",
            attributes={"plugin": "meiva_code", "surface": "hooks"},
        ),
        world.event_row(
            4,
            world.ts_after(40),
            event_name="plugin_activation",
            attributes={"plugin": "meiva_code", "surface": "hooks", "identifier": "before_exec"},
        ),
        world.event_row(5, world.ts_after(50), agent_id=99, event_name="turn_end"),
        world.event_row(
            6,
            world.ts_after(60),
            category="audit",
            event_name="skill_invoked",
            attributes={
                "skill": "meiva-goal",
                "identifier": "meiva-goal",
                "invocation_depth": "loaded",
            },
        ),
    ]
    with db_conn.cursor() as cur:
        world.insert_agent(cur, 7, status="terminated", last_message="delivered")
        world.insert_inbound(cur, 7, "user", "do the task", "1 hour")
    db_conn.commit()

    with world.serve_events(rows) as server:
        rec = collect_mod.collect_one(
            7, "2026-08-13", None, database=database, gateway=world.grant(server.url), now=now
        )

    assert rec["agent_id"] == 7
    assert rec["turns"] == 1
    assert rec["tokens_in"] == 100
    assert rec["task_prompt"] == "do the task"
    assert rec["skills_touched"] == ["meiva-goal"]
    assert rec["plugins_activated"] == {"meiva_code/hooks/before_exec": 1}
    assert rec["plugins_activated_skipped"] == 1
    assert rec["evidence_grade"] == "complete"
    assert rec["subprocess_calls"] == 0
    assert "agent 7: skipped 1 malformed plugin_activation row(s)" in capsys.readouterr().out
    assert {r.params["agent_id"] for r in server.requests} == {"7"}


def test_collect_one_refuses_an_agent_without_a_lifecycle_row(
    collect_mod: Any, database: Database
) -> None:
    with (
        world.serve_events([]) as server,
        pytest.raises(ValueError, match="no agents_meta row for agent 5"),
    ):
        collect_mod.collect_one(
            5,
            "2026-08-13",
            None,
            database=database,
            gateway=world.grant(server.url),
            now=world.ts_after(3600),
        )


# ── plugin attribution (issue #40) ──────────────────────────────────────────


def test_plugins_activated_counts_per_contribution() -> None:
    """The plugin-side counterpart of `skills_touched`: hooks and wraps act
    silently, so without this the dataset carried no evidence that a plugin
    touched a bad run. Keyed per contribution, not per plugin, so a regression
    points at the hook to read. Well-formed rows produce a zero skip count."""
    events = [
        (
            "plugin_activation",
            {
                "plugin": "meiva_syntax_fix",
                "surface": "hooks",
                "identifier": "before_exec",
                "detail": "_SyntaxFixHook wrote messages",
                "model": "m",
            },
        ),
        (
            "plugin_activation",
            {
                "plugin": "meiva_syntax_fix",
                "surface": "hooks",
                "identifier": "before_exec",
                "detail": "",
                "model": "m",
            },
        ),
        (
            "plugin_activation",
            {
                "plugin": "meiva_code",
                "surface": "sdkWraps",
                "identifier": "files.read",
                "detail": "inner_calls=0",
                "model": "m",
            },
        ),
        ("turn_end", {"ok": True}),
    ]

    fired, skipped = record_mod._plugins_activated(record_mod.event_facts(events))
    assert fired == {
        "meiva_code/sdkWraps/files.read": 1,
        "meiva_syntax_fix/hooks/before_exec": 2,
    }
    assert skipped == 0


def test_plugins_activated_counts_and_reports_malformed_rows() -> None:
    """Issue #92: a row missing part of the key is still dropped (graceful
    drift — it never crashes the weekly collect), but the drop is now counted
    instead of vanishing silently, so an event-contract drift is visible on
    the run record rather than quietly thinning the dataset to empty."""
    events = [
        ("plugin_activation", {"plugin": "meiva_code", "surface": "hooks"}),
        ("plugin_activation", {}),
        ("plugin_activation", {"identifier": "before_exec"}),
    ]

    fired, skipped = record_mod._plugins_activated(record_mod.event_facts(events))
    assert fired == {}
    assert skipped == 3


# meiva:note
# Issue #92: the skip count must land on the run record, not just a private counter.
def test_build_record_surfaces_plugins_activated_skipped() -> None:
    events = [
        ("plugin_activation", {"plugin": "meiva_code", "surface": "hooks"}),  # malformed
        (
            "plugin_activation",
            {"plugin": "meiva_code", "surface": "hooks", "identifier": "before_exec"},
        ),
    ]

    rec = _record(events=tuple(events))

    assert rec["plugins_activated_skipped"] == 1
    assert rec["plugins_activated"] == {"meiva_code/hooks/before_exec": 1}


# meiva:note
# Well-formed rows must not be affected by the loud-skip change: zero skipped.
def test_build_record_well_formed_rows_report_zero_skipped() -> None:
    events = [
        (
            "plugin_activation",
            {"plugin": "meiva_code", "surface": "hooks", "identifier": "before_exec"},
        ),
    ]

    rec = _record(events=tuple(events))

    assert rec["plugins_activated_skipped"] == 0
    assert rec["plugins_activated"] == {"meiva_code/hooks/before_exec": 1}


# meiva:note
# Only a `skill_invoked` row whose payload validates with invocation depth `loaded` counts.
def test_loaded_skills_keeps_only_validated_loads(collect_mod: Any) -> None:
    log_events = [
        ("skill_invoked", {"skill": "b", "identifier": "b", "invocation_depth": "loaded"}),
        ("skill_invoked", {"skill": "a", "identifier": "a", "invocation_depth": "loaded"}),
        ("skill_invoked", {"skill": "a", "identifier": "a", "invocation_depth": "loaded"}),
        ("skill_invoked", {"skill": "c", "identifier": "c", "invocation_depth": "listed"}),
        ("skill_invoked", {"skill": "d"}),
        ("compact", {"skill": "e", "identifier": "e", "invocation_depth": "loaded"}),
    ]

    assert collect_mod.loaded_skills(log_events) == ["a", "b"]


def test_task_receipts_by_agent_counts_notes_states_and_changes(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    receipts = [
        (7001, 501, "k1", {"note": "still working"}),
        (7001, 501, "k2", {"note": "still working"}),
        (7001, 501, "k3", {"status": "in_progress"}),
        (7001, 501, "k4", {"status": "done"}),
        (7001, 501, "k5", {"status": "done"}),
        (7001, 501, "k6", {"results": "shipped"}),
        (7001, 501, "k7", {"status": "in_progress"}),
        (7001, 502, "k8", {"status": "done", "note": "closed"}),
        (7002, 503, "k9", {"note": "unrelated actor"}),
    ]
    with db_conn.cursor() as cur:
        for offset, (actor, task_id, key, request) in enumerate(receipts):
            cur.execute(
                "INSERT INTO task_update_receipts "
                "(actor_agent_id, task_id, operation_key, request, accepted_at) "
                "VALUES (%s, %s, %s, %s, now() + %s * interval '1 second')",
                (actor, task_id, key, Jsonb(request), offset),
            )
    db_conn.commit()

    with db_conn.cursor() as cur:
        counts = collect_mod._task_receipts_by_agent(cur, [7001, 7002, 7999])

    assert counts[7001] == record_mod.TaskReceipts(2, 5, 1, 3)
    assert counts[7002] == record_mod.TaskReceipts(1, 0, 0, 0)
    assert 7999 not in counts
