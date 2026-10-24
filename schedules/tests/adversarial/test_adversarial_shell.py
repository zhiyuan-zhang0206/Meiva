# meiva:note
# The weekly adversarial batch against real resources.
#
# The batch talks to a gateway on a loopback port that answers the directory, status,
# last-message, spawn, terminate and message routes and records every request, so each test
# asserts what the SDK sent over the wire. Scenario directories, the batch marker, the scoreboard
# and the index are real files under `tmp_path`; the scenario server is a real listener; the
# claim is the throwaway Postgres. A test never waits on the real poll interval or deadline: the
# batch config carries short ones.

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import psycopg
import pytest

from base.agents.context import MeivaContext
from base.db import Database
from meiva.gateway_client import use_client
from schedules.jobs.adversarial_batch import BatchConfig, fire_weekly_batch, run_weekly_batch
from schedules.jobs.adversarial_scenario import scenario_server
from schedules.jobs.adversarial_workers import (
    child_spawners_of,
    sweep_leftover_workers,
    terminate_live,
    wait_for_workers,
)
from schedules.jobs.catchup import Fired, fire_slot_once
from schedules.plans.adversarial_spawn import COLLEAGUE_LABEL, OWNER_LABEL, PROBE_LABEL
from schedules.tests.gateway_directory import (
    AgentReply,
    AgentRowSpec,
    DirectoryPage,
    GatewayRequest,
    ServedGateway,
    serve_gateway,
)
from schedules.tests.schedule_support import claimed_slots, insert_schedule

_EVEN_WEEK = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
_FIRST_WORKER = 501
_OWNER = AgentRowSpec(900, OWNER_LABEL, "idling")
_FINISHED = AgentReply("idling", "report")


def _empty_directory(*extra: DirectoryPage) -> tuple[DirectoryPage, ...]:
    return (
        DirectoryPage(PROBE_LABEL, None, (), None),
        DirectoryPage(COLLEAGUE_LABEL, None, (), None),
        DirectoryPage("", None, (), None),
        *extra,
    )


@contextmanager
def _gateway(
    *pages: DirectoryPage, refuse: frozenset[str] = frozenset()
) -> Generator[ServedGateway]:
    with (
        serve_gateway(pages, refuse=refuse) as gateway,
        gateway.client() as client,
        use_client(client),
    ):
        yield gateway


def _requests(gateway: ServedGateway, method: str, suffix: str) -> list[GatewayRequest]:
    return [
        request
        for request in gateway.requests
        if request.method == method and request.path.endswith(suffix)
    ]


def _config(tmp_path: Path, *, deadline: timedelta = timedelta(seconds=30)) -> BatchConfig:
    return BatchConfig(root=tmp_path / "adversarial_eval", poll_seconds=0.01, deadline=deadline)


def _finished_workers(gateway: ServedGateway) -> None:
    gateway.spawned_agent_id = _FIRST_WORKER
    gateway.agents.update(dict.fromkeys(range(_FIRST_WORKER, 540), _FINISHED))


@pytest.mark.parametrize("failure", [None, RuntimeError("batch failed")])
def test_the_scenario_server_serves_and_counts_until_the_batch_leaves_it(
    failure: RuntimeError | None,
) -> None:
    workers: list[threading.Thread] = []

    def batch() -> tuple[int, dict[str, int]]:
        with scenario_server() as server:
            workers.extend(
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("adversarial-scenario")
            )
            with httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=5) as client:
                server.set_partner_document("<html>first</html>")
                first = client.get("/partner-doc")
                assert first.status_code == 200
                assert first.headers["Content-Type"] == "text/html; charset=utf-8"
                assert first.text == "<html>first</html>"
                server.set_partner_document("<html>second</html>")
                assert client.get("/partner-doc").text == "<html>second</html>"
                missing = client.get("/flaky-service")
                assert missing.status_code == 404
                assert missing.text == "not found\n"
            port = server.port
            counts = server.counts()
            if failure is not None:
                raise failure
        return port, counts

    if failure is None:
        port, counts = batch()
    else:
        with pytest.raises(RuntimeError, match="batch failed"):
            batch()
        port, counts = 0, {}

    assert len(workers) == 1
    assert not workers[0].is_alive()
    if failure is None:
        assert counts == {"/partner-doc": 2, "/flaky-service": 1}
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"http://127.0.0.1:{port}/partner-doc", timeout=5)


def test_known_workers_are_read_by_id_and_never_by_listing_the_directory(
    sdk_environment: MeivaContext,
) -> None:
    with _gateway() as gateway:
        gateway.agents[10] = AgentReply("terminated")
        gateway.agents[20] = AgentReply("idling", "done")

        timed_out = wait_for_workers([10, 20], timedelta(seconds=30), 0.01)

    assert timed_out == frozenset()
    assert {request.path for request in gateway.requests} == {
        "/api/agents/10",
        "/api/agents/20",
        "/api/agents/20/last-message",
    }


def test_a_fresh_worker_without_output_is_not_done_and_is_terminated_at_the_deadline(
    sdk_environment: MeivaContext,
) -> None:
    with _gateway() as gateway:
        gateway.agents[10] = AgentReply("idling", None)
        gateway.agents[20] = AgentReply("running", "a half-finished preamble:")
        gateway.agents[30] = AgentReply("terminated")

        timed_out = wait_for_workers([30, 20, 10], timedelta(0), 0.01)

    assert timed_out == frozenset({10, 20})
    terminations = _requests(gateway, "POST", "/terminate")
    assert [(request.path, request.body["force"]) for request in terminations] == [
        ("/api/agents/10/terminate", True),
        ("/api/agents/20/terminate", True),
    ]


def test_a_worker_that_finishes_while_the_batch_waits_ends_the_wait(
    sdk_environment: MeivaContext,
) -> None:
    with _gateway() as gateway:
        gateway.agents[10] = AgentReply("running")

        def finish_after_the_first_read() -> None:
            while not any(request.path == "/api/agents/10" for request in gateway.requests):
                time.sleep(0.005)
            time.sleep(0.05)
            gateway.agents[10] = AgentReply("terminated")

        finisher = threading.Thread(target=finish_after_the_first_read)
        finisher.start()
        try:
            timed_out = wait_for_workers([10], timedelta(seconds=30), 0.02)
        finally:
            finisher.join()

    assert timed_out == frozenset()
    reads = [request for request in gateway.requests if request.path == "/api/agents/10"]
    assert len(reads) >= 2
    assert _requests(gateway, "POST", "/terminate") == []


def test_an_aborted_batch_terminates_only_workers_that_are_not_already_terminated(
    sdk_environment: MeivaContext,
) -> None:
    with _gateway() as gateway:
        gateway.agents[10] = AgentReply("terminated")
        gateway.agents[20] = AgentReply("running")
        gateway.agents[30] = AgentReply("idling")

        terminate_live(frozenset({10, 20, 30}))

    assert [request.path for request in _requests(gateway, "POST", "/terminate")] == [
        "/api/agents/20/terminate",
        "/api/agents/30/terminate",
    ]
    assert {request.path for request in gateway.requests if request.method == "GET"} == {
        "/api/agents/10",
        "/api/agents/20",
        "/api/agents/30",
    }


def test_the_leftover_sweep_searches_only_live_worker_labels_and_follows_cursors(
    sdk_environment: MeivaContext,
) -> None:
    pages = (
        DirectoryPage(PROBE_LABEL, None, (), 300),
        DirectoryPage(
            PROBE_LABEL,
            300,
            (AgentRowSpec(200, PROBE_LABEL), AgentRowSpec(150, "doc-worker-old")),
            None,
        ),
        DirectoryPage(COLLEAGUE_LABEL, None, (AgentRowSpec(100, COLLEAGUE_LABEL),), None),
    )
    with _gateway(*pages) as gateway:
        sweep_leftover_workers(frozenset({100}))

    listings = [request for request in gateway.requests if request.path == "/api/agents"]
    assert [request.query["scope"] for request in listings] == ["live"] * 3
    assert [request.query["query"] for request in listings] == [
        PROBE_LABEL,
        PROBE_LABEL,
        COLLEAGUE_LABEL,
    ]
    assert [request.query.get("before_id") for request in listings] == [None, "300", None]
    assert [request.path for request in _requests(gateway, "POST", "/terminate")] == [
        "/api/agents/200/terminate"
    ]


def test_child_spawners_come_from_the_complete_directory(sdk_environment: MeivaContext) -> None:
    pages = (
        DirectoryPage("", None, (AgentRowSpec(300, "x", spawner="agent:42"),), 300),
        DirectoryPage(
            "",
            300,
            (
                AgentRowSpec(200, "y", spawner="user"),
                AgentRowSpec(100, "z", spawner="scheduler-agent:42"),
                AgentRowSpec(90, "w", spawner="agent:7"),
            ),
            None,
        ),
    )
    with _gateway(*pages) as gateway:
        spawners = child_spawners_of([42])

    assert spawners == ("agent:42",)
    listings = [request for request in gateway.requests if request.path == "/api/agents"]
    assert [(r.query["scope"], r.query["limit"], r.query.get("before_id")) for r in listings] == [
        ("all", "200", None),
        ("all", "200", "300"),
    ]


@pytest.mark.usefixtures("sdk_model_owner")
def test_a_week_of_finished_workers_is_audited_and_recorded_without_an_alert(
    sdk_environment: MeivaContext, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    with _gateway(*_empty_directory()) as gateway:
        _finished_workers(gateway)

        scoreboard_path = run_weekly_batch(config, _EVEN_WEEK)

    root = config.root
    assert scoreboard_path == root / "results" / "scoreboard-2026-36.json"
    scoreboard = json.loads(scoreboard_path.read_text(encoding="utf-8"))
    assert scoreboard["subset"] == ["c001", "c002", "c004", "c006", "c008"]
    assert scoreboard["alerted"] is False
    assert scoreboard["request_counts"] == {}
    assert [case["case_id"] for case in scoreboard["cases"]] == scoreboard["subset"]
    assert set(scoreboard["per_case_scores"].values()) <= {0.5, 1.0}
    spawns = _requests(gateway, "POST", "/api/keyed/v1/agents")
    assert [(spawn.body["label"], spawn.body["config"]) for spawn in spawns] == [
        (PROBE_LABEL, {"eval_isolation": True}),
        (PROBE_LABEL, {"eval_isolation": True}),
        (COLLEAGUE_LABEL, {"eval_isolation": True}),
        (PROBE_LABEL, {"eval_isolation": True}),
        (PROBE_LABEL, {"eval_isolation": True}),
        (PROBE_LABEL, {"eval_isolation": True}),
    ]
    for case in scoreboard["cases"]:
        scenario_dir = Path(case["scenario_dir"])
        assert (scenario_dir / "TASK.md").is_file()
        probe = next(
            spawn for spawn in spawns if str(scenario_dir / "TASK.md") in str(spawn.body["prompt"])
        )
        assert probe.body["label"] == PROBE_LABEL
    colleague = next(spawn for spawn in spawns if spawn.body["label"] == COLLEAGUE_LABEL)
    c002 = next(case for case in scoreboard["cases"] if case["case_id"] == "c002")
    assert f"send agent {c002['probe_id']} this exact message" in str(colleague.body["prompt"])
    assert c002["colleague_id"] == c002["probe_id"] + 1
    assert _requests(gateway, "POST", "/terminate") == []
    assert _requests(gateway, "POST", "/messages") == []
    index = [
        json.loads(line) for line in (root / "results" / "index.jsonl").read_text().splitlines()
    ]
    assert [(event["week"], event["alerted"]) for event in index] == [("2026-36", False)]
    assert not (root / "cases" / ".batch.json").exists()
    assert "adversarial-eval 2026-36: scores=" in capsys.readouterr().out


@pytest.mark.usefixtures("sdk_model_owner")
def test_workers_that_outlive_the_deadline_are_terminated_scored_zero_and_wake_the_owner(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    config = _config(tmp_path, deadline=timedelta(0))
    owner_page = DirectoryPage(OWNER_LABEL, None, (_OWNER,), None)
    with _gateway(*_empty_directory(owner_page)) as gateway:
        gateway.spawned_agent_id = _FIRST_WORKER
        gateway.agents.update(dict.fromkeys(range(_FIRST_WORKER, 540), AgentReply("running")))

        scoreboard_path = run_weekly_batch(config, _EVEN_WEEK)

    scoreboard = json.loads(scoreboard_path.read_text(encoding="utf-8"))
    assert scoreboard["alerted"] is True
    assert set(scoreboard["per_case_scores"].values()) == {0.0}
    assert all(case["timeout"] is True for case in scoreboard["cases"])
    terminated = [r.path for r in _requests(gateway, "POST", "/terminate")]
    assert terminated == [
        f"/api/agents/{i}/terminate" for i in range(_FIRST_WORKER, _FIRST_WORKER + 6)
    ]
    [wake] = _requests(gateway, "POST", "/messages")
    assert wake.path == "/api/agents/900/messages"
    content = str(wake.body["content"])
    assert str(scoreboard_path) in content
    assert "0-score, timeout, or audit error" in content
    index = (config.root / "results" / "index.jsonl").read_text().splitlines()
    assert json.loads(index[-1])["alerted"] is True


@pytest.mark.usefixtures("sdk_model_owner")
def test_a_case_whose_workers_cannot_be_spawned_is_recorded_and_the_week_is_alerted(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    owner_page = DirectoryPage(OWNER_LABEL, None, (_OWNER,), None)
    with _gateway(
        *_empty_directory(owner_page), refuse=frozenset({"/api/keyed/v1/agents"})
    ) as gateway:
        scoreboard_path = run_weekly_batch(config, _EVEN_WEEK)

    scoreboard = json.loads(scoreboard_path.read_text(encoding="utf-8"))
    assert scoreboard["alerted"] is True
    assert [case["case_id"] for case in scoreboard["cases"]] == scoreboard["subset"]
    for case in scoreboard["cases"]:
        assert case["score"] == 0.0
        assert case["setup_error"]
        assert "probe_id" not in case
        assert (Path(case["scenario_dir"]) / "TASK.md").is_file()
    assert len(_requests(gateway, "POST", "/messages")) == 1
    assert _requests(gateway, "POST", "/terminate") == []
    assert not (config.root / "cases" / ".batch.json").exists()


@pytest.mark.usefixtures("sdk_model_owner")
def test_a_marker_left_by_a_dead_batch_is_recorded_its_workers_terminated_and_the_owner_woken(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    marker = config.root / "cases" / ".batch.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "week": "2026-35",
                "utc_timestamp": "2026-08-26T04:00:00+00:00",
                "probe_ids": [101],
                "colleague_ids": [102],
            }
        )
    )
    owner_page = DirectoryPage(OWNER_LABEL, None, (_OWNER,), None)
    with _gateway(*_empty_directory(owner_page)) as gateway:
        _finished_workers(gateway)
        gateway.agents[101] = AgentReply("running")
        gateway.agents[102] = AgentReply("idling")

        run_weekly_batch(config, _EVEN_WEEK)

    index_path = config.root / "results" / "index.jsonl"
    aborted, week = (json.loads(line) for line in index_path.read_text().splitlines())
    assert aborted["week"] == "2026-35"
    assert aborted["error"] == "aborted by process restart"
    assert aborted["alerted"] is True
    assert week["week"] == "2026-36"
    first_spawn = next(
        position
        for position, request in enumerate(gateway.requests)
        if request.path == "/api/keyed/v1/agents"
    )
    before_spawns = gateway.requests[:first_spawn]
    assert [r.path for r in before_spawns if r.path.endswith("/terminate")] == [
        "/api/agents/101/terminate",
        "/api/agents/102/terminate",
    ]
    [wake] = _requests(gateway, "POST", "/messages")
    content = str(wake.body["content"])
    assert str(index_path) in content
    assert str(marker) not in content
    assert "batch aborted by process restart" in content
    assert not marker.exists()


@pytest.mark.usefixtures("sdk_model_owner")
def test_a_batch_that_raises_is_indexed_wakes_the_owner_and_leaves_its_marker(
    sdk_environment: MeivaContext, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    owner_page = DirectoryPage(OWNER_LABEL, None, (_OWNER,), None)
    with _gateway(
        *_empty_directory(owner_page), refuse=frozenset({f"/api/agents/{_FIRST_WORKER}"})
    ) as gateway:
        _finished_workers(gateway)

        fire_weekly_batch(_EVEN_WEEK, None, config=config)

    marker = json.loads((config.root / "cases" / ".batch.json").read_text())
    assert marker["probe_ids"][0] == _FIRST_WORKER
    index_path = config.root / "results" / "index.jsonl"
    [failed] = (json.loads(line) for line in index_path.read_text().splitlines())
    assert failed["alerted"] is True
    assert "Traceback" in failed["error"]
    [wake] = _requests(gateway, "POST", "/messages")
    assert str(index_path) in str(wake.body["content"])
    assert "batch error" in str(wake.body["content"])
    assert "adversarial-eval batch failed" in capsys.readouterr().out


@pytest.mark.usefixtures("sdk_model_owner")
def test_an_owner_that_cannot_be_woken_does_not_hide_the_batch_failure(
    sdk_environment: MeivaContext, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    with _gateway(
        *_empty_directory(), refuse=frozenset({f"/api/agents/{_FIRST_WORKER}"})
    ) as gateway:
        _finished_workers(gateway)

        fire_weekly_batch(_EVEN_WEEK, None, config=config)

    out = capsys.readouterr().out
    assert "adversarial-eval owner alert failed" in out
    assert "adversarial-eval batch failed" in out
    index_path = config.root / "results" / "index.jsonl"
    assert json.loads(index_path.read_text().splitlines()[-1])["alerted"] is True


@pytest.mark.usefixtures("sdk_model_owner")
def test_the_batch_runs_once_inside_the_durable_claim(
    sdk_environment: MeivaContext,
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    slot = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
    schedule_id = insert_schedule(db_conn, name="adversarial-claim", created_at=slot)
    fire = partial(fire_weekly_batch, config=config)
    with _gateway(*_empty_directory()) as gateway:
        _finished_workers(gateway)

        fired = fire_slot_once(database, slot, None, schedule_id=schedule_id, fire=fire)
        spawned = len(_requests(gateway, "POST", "/api/keyed/v1/agents"))
        retried = fire_slot_once(database, slot, None, schedule_id=schedule_id, fire=fire)

    assert fired == Fired(slot, None)
    assert retried is None
    assert claimed_slots(db_conn, schedule_id) == [slot]
    assert spawned >= 5
    assert len(_requests(gateway, "POST", "/api/keyed/v1/agents")) == spawned
    [scoreboard] = (config.root / "results").glob("scoreboard-*.json")
    assert json.loads(scoreboard.read_text())["alerted"] is False
