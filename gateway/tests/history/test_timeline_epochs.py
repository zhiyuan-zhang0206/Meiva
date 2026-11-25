from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from base.agents.history.timeline_item import TimelineItem
from base.db import create_agent
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client, reconfigure
from gateway.tests.history.test_timeline import CompactHistoryCases
from ops.cluster.rpc import ClusterOpUnreachable

_AT = "2026-11-25T{hour:02d}:30:00+00:00"


class EpochRunner:
    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any] | Exception]) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def script(self, reply: dict[str, Any] | Exception) -> None:
        def scripted(_payload: dict[str, Any]) -> dict[str, Any] | Exception:
            return reply

        self.answer = scripted

    async def dispatch_to_machine(
        self, db: object, target_machine: str, kind: str, payload: dict[str, Any], **_: object
    ) -> dict[str, Any]:
        if kind == "lifecycle":
            return {"status": "spawned"}
        self.calls.append(payload)
        reply = self.answer(payload)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


def _external_item(item_id: str, payload: str, hour: int) -> dict[str, Any]:
    return TimelineItem(
        item_id=item_id, kind="agent_chat", payload=payload, created_at=_AT.format(hour=hour)
    ).model_dump(mode="json")


def _window(*items: dict[str, Any], more: bool = False, size: int = 9000) -> dict[str, Any]:
    return {"items": list(items), "has_more": more, "size": size}


def _human(text: str, hour: int) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_source": "user",
            "meiva_created_at": _AT.format(hour=hour),
        },
    )


def _reply(text: str, hour: int) -> AIMessage:
    return AIMessage(content=text, additional_kwargs={"meiva_created_at": _AT.format(hour=hour)})


def _epochs(conn: psycopg.Connection, agent: int, rows: list[tuple[Any, ...]]) -> None:
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) VALUES (%s, 'macmini', 'idling', %s) "
        "ON CONFLICT (id) DO UPDATE SET harness = EXCLUDED.harness, machine = EXCLUDED.machine",
        (agent, rows[-1][1]),
    )
    for seq, harness, start, end, source_start, source_end, session, reason in rows:
        conn.execute(
            "INSERT INTO agent_harness_epochs (agent_id, seq, harness, model, session_id, started_at, "
            "ended_at, source_start, source_end, switched_by, reason) "
            "VALUES (%s, %s, %s, 'model-x', %s, %s, %s, %s, %s, %s, %s)",
            (
                agent,
                seq,
                harness,
                session,
                _AT.format(hour=start),
                None if end is None else _AT.format(hour=end),
                source_start,
                source_end,
                "birth" if seq == 0 else "user",
                reason,
            ),
        )
    conn.commit()


def _ids(page: dict[str, Any]) -> list[str]:
    return [item["item_id"] for item in page["items"]]


class TestTimelineEpochs(CompactHistoryCases):
    def _meiva_cli_meiva(self, conn: psycopg.Connection) -> int:
        agent = create_agent(conn)
        _epochs(
            conn,
            agent,
            [
                (0, "meiva", 1, 3, None, None, None, None),
                (1, "claude_code", 3, 5, 0, 5000, "session-1", "see /tmp/handoff.md"),
                (2, "meiva", 5, None, None, None, None, "back to native"),
            ],
        )
        self._put_checkpoint(
            agent,
            [
                SystemMessage(content="You are agent."),
                _human("first question", 2),
                _reply("first answer", 2),
                _human("second question", 6),
                _reply("second answer", 6),
            ],
            version="1",
        )
        return agent

    def test_the_head_is_the_current_native_epoch_and_its_switch_block(
        self, db_conn: psycopg.Connection, client: GatewayTestClient
    ) -> None:
        agent = self._meiva_cli_meiva(db_conn)

        page = client.get(f"/api/agents/{agent}/timeline").json()

        assert _ids(page) == ["e2.sw", "3.0", "4.0"]
        assert (page["current_epoch"], page["msg_count"], page["has_more"]) == (2, 5, True)
        block = page["items"][0]
        assert (block["kind"], block["source"], block["show_timestamp"]) == (
            "system_marker",
            "harness_switch",
            True,
        )
        assert block["payload"] == (
            "Switched harness: claude_code -> meiva (model-x)\n\nRestart message: back to native"
        )

    def test_paging_from_the_head_walks_back_through_every_epoch(
        self, db_conn: psycopg.Connection, client: GatewayTestClient
    ) -> None:
        agent = self._meiva_cli_meiva(db_conn)
        runner = EpochRunner(lambda _: _window(_external_item("h100.0", "cli reply", 4), size=5000))
        client.app.state.rpc = runner

        cli = client.get(f"/api/agents/{agent}/timeline", params={"before": "e2.sw"}).json()
        assert _ids(cli) == ["e1.sw", "e1.h100.0"]
        assert (cli["has_more"], cli["msg_count"], cli["current_epoch"]) == (True, 0, 2)
        assert cli["items"][0]["payload"] == (
            "Switched harness: meiva -> claude_code (model-x)\n\nRestart message: see /tmp/handoff.md"
        )
        assert runner.calls[-1]["before"] == 5000
        assert runner.calls[-1]["floor"] == 0
        assert runner.calls[-1]["session_id"] == "session-1"

        native = client.get(f"/api/agents/{agent}/timeline", params={"before": "e1.sw"}).json()
        assert _ids(native) == ["e0.0.0", "e0.1.0", "e0.2.0"]
        assert native["has_more"] is False
        assert [item["payload"] for item in native["items"]][1:] == [
            "first question",
            "first answer",
        ]

    def test_the_cursor_of_the_first_epochs_head_is_a_final_empty_page(
        self, db_conn: psycopg.Connection, client: GatewayTestClient
    ) -> None:
        agent = self._meiva_cli_meiva(db_conn)

        end = client.get(f"/api/agents/{agent}/timeline", params={"before": "e0.sw"}).json()
        assert (end["items"], end["has_more"]) == ([], False)

    def test_an_offline_machine_skips_only_its_epoch(
        self,
        db_conn: psycopg.Connection,
        client: GatewayTestClient,
        loguru_records: LogRecords,
    ) -> None:
        agent = self._meiva_cli_meiva(db_conn)
        client.app.state.rpc = EpochRunner(lambda _: ClusterOpUnreachable("down"))

        page = client.get(f"/api/agents/{agent}/timeline", params={"before": "e2.sw"})

        assert page.status_code == 200
        body = page.json()
        assert _ids(body) == ["e1.sw"]
        assert body["has_more"] is True
        (warning,) = loguru_records.extras(event="timeline_epoch_unreadable")
        assert (warning["agent_id"], warning["epoch"], warning["harness"]) == (
            agent,
            1,
            "claude_code",
        )
        assert warning["reason"] == "the agent's machine is offline"
        older = client.get(f"/api/agents/{agent}/timeline", params={"before": "e1.sw"}).json()
        assert _ids(older) == ["e0.0.0", "e0.1.0", "e0.2.0"]

    @pytest.mark.parametrize(
        "unreadable", ["session_mismatch", "file_missing", "undecodable", "no_session"]
    )
    def test_an_unreadable_record_of_an_earlier_epoch_is_skipped(
        self,
        db_conn: psycopg.Connection,
        client: GatewayTestClient,
        unreadable: str,
    ) -> None:
        agent = self._meiva_cli_meiva(db_conn)
        reply = {"items": [], "has_more": False, "size": 0, "unreadable": unreadable}
        client.app.state.rpc = EpochRunner(lambda _: reply)

        body = client.get(f"/api/agents/{agent}/timeline", params={"before": "e2.sw"}).json()

        assert _ids(body) == ["e1.sw"]
        assert body["has_more"] is True

    def test_the_current_external_epoch_reads_from_its_offset_and_a_missing_record_is_empty(
        self, db_conn: psycopg.Connection, client: GatewayTestClient
    ) -> None:
        agent = create_agent(db_conn)
        _epochs(
            db_conn,
            agent,
            [
                (0, "claude_code", 1, 3, 0, 700, "session-1", None),
                (1, "meiva", 3, 5, None, None, None, None),
                (2, "claude_code", 5, None, 700, None, None, "again"),
            ],
        )
        runner = EpochRunner(lambda _: _window(_external_item("h900.0", "new reply", 6)))
        client.app.state.rpc = runner

        head = client.get(f"/api/agents/{agent}/timeline").json()
        assert _ids(head) == ["e2.sw", "h900.0"]
        assert (head["current_epoch"], head["msg_count"]) == (2, 9000)
        assert runner.calls[-1]["floor"] == 700
        assert "before" not in runner.calls[-1] or runner.calls[-1]["before"] is None
        assert "session_id" not in runner.calls[-1]

        runner.script({"items": [], "has_more": False, "size": 0, "unreadable": "no_session"})
        fresh = client.get(f"/api/agents/{agent}/timeline").json()
        assert _ids(fresh) == ["e2.sw"]

        runner.script(_window(_external_item("h100.0", "old reply", 2), size=700))
        first = client.get(f"/api/agents/{agent}/timeline", params={"before": "e1.sw"}).json()
        assert _ids(first) == ["e0.h100.0"]
        assert first["has_more"] is False
        assert runner.calls[-1]["before"] == 700
        assert runner.calls[-1]["session_id"] == "session-1"

    def test_retained_history_does_not_take_an_epoch_cursor(
        self, db_conn: psycopg.Connection, client: GatewayTestClient
    ) -> None:
        agent = self._meiva_cli_meiva(db_conn)

        response = client.get(
            f"/api/agents/{agent}/timeline/retained", params={"before": "e0.s1.cp.3.0"}
        )

        assert response.status_code == 400

    def test_compacted_history_is_sliced_by_epoch_across_its_retained_segments(
        self, db_conn: psycopg.Connection, client: GatewayTestClient
    ) -> None:
        agent = create_agent(db_conn)
        _epochs(
            db_conn,
            agent,
            [
                (0, "meiva", 1, 3, None, None, None, None),
                (1, "claude_code", 3, 5, 0, 5000, "session-1", None),
                (2, "meiva", 5, None, None, None, None, None),
            ],
        )
        boundary = self._put_checkpoint(
            agent,
            [
                SystemMessage(content="system"),
                self._summary("early summary"),
                _human("early question", 2),
                _reply("early answer", 2),
            ],
            version="1",
            boundary=True,
        )
        self._put_checkpoint(
            agent,
            self._current(_human("late question", 6), _reply("late answer", 6)),
            version="2",
        )
        reconfigure(client, timeline_compact_history=-1)

        head = client.get(f"/api/agents/{agent}/timeline").json()
        assert _ids(head) == ["2.0", "3.0"]
        assert head["has_more"] is True

        seam = client.get(f"/api/agents/{agent}/timeline", params={"before": "2.0"}).json()
        assert _ids(seam) == ["e2.sw"]
        assert seam["has_more"] is True

        native = client.get(f"/api/agents/{agent}/timeline", params={"before": "e1.sw"}).json()
        assert _ids(native) == ["e0.0.0", "e0.1.0"]
        assert native["has_more"] is True

        retained = client.get(f"/api/agents/{agent}/timeline", params={"before": "e0.0.0"}).json()
        assert _ids(retained) == [
            f"e0.s1.{boundary}.0.0",
            f"e0.s1.{boundary}.1.0",
            f"e0.s1.{boundary}.2.0",
        ]
        assert retained["has_more"] is False
