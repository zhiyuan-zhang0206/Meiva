from __future__ import annotations

import psycopg
import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from base.config import settings
from base.db import create_agent
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    reconfigure,
)
from gateway.tests.history.test_timeline import CompactHistoryCases
from gateway.tests.history.test_timeline import test_client as test_client
from gateway.tests.support.checkpoints.unreadable import put_unreadable_head


class TestTimelineCompactHistoryHeadNotes(CompactHistoryCases):
    @pytest.mark.parametrize(
        "before",
        [
            f"{'1' * 5000}.0",
            f"s1.boundary.1.{'0' * 5000}",
        ],
    )
    def test_oversized_numeric_cursor_is_terminal_not_500(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
        before: str,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        reconfigure(test_client, timeline_compact_history=-1)

        response = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": before, "limit": 50},
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "msg_count": 3, "has_more": False}

    def test_missing_cross_segment_target_is_terminal_even_if_an_older_boundary_exists(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("older", 1), version="1", boundary=True)
        # meiva:note
        # The newer boundary exists in the index but holds no messages: its segment is gone.
        self._put_checkpoint(tid, [], version="2", boundary=True)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        reconfigure(test_client, timeline_compact_history=-1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": "2.0", "limit": 50},
        )

        assert page.status_code == 200
        assert [item["item_id"] for item in page.json()["items"]] == ["0.0", "1.0"]
        assert page.json()["msg_count"] == 3
        assert page.json()["has_more"] is False

    def test_standing_note_cursor_crosses_from_current_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage, HumanMessage

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 1), version="1", boundary=True
        )
        standing_note = HumanMessage(
            content="remember this",
            additional_kwargs={
                "meiva_msg_type": "system_note",
                "meiva_note_tag": "memory",
                "meiva_created_at": "2026-08-25T00:00:01+00:00",
            },
        )
        self._put_checkpoint(
            tid,
            self._current(standing_note, AIMessage(content="current item")),
            version="2",
        )
        reconfigure(test_client, timeline_compact_history=1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "2.0", "limit": 50}
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert page["has_more"] is False

    @pytest.mark.parametrize("damage", ["read_error", "missing", "malformed_message"])
    def test_failed_read_is_unavailable_but_disappeared_segment_is_terminal(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
        damage: str,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        if damage == "read_error":
            checkpoint_id = put_unreadable_head(
                settings.data_plane.db_url, tid, version="1", boundary=True
            )
        else:
            segment: list[BaseMessage] = (
                []
                if damage == "missing"
                else [
                    SystemMessage(content="system"),
                    HumanMessage(
                        content="damaged inbound",
                        additional_kwargs={
                            "meiva_msg_type": "inbound",
                            "meiva_inbound_id": "not-an-integer",
                        },
                    ),
                ]
            )
            checkpoint_id = self._put_checkpoint(tid, segment, version="1", boundary=True)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )
        reconfigure(test_client, timeline_compact_history=-1)

        url = f"/api/agents/{tid}/timeline"
        params = {"before": f"s1.{checkpoint_id}.1.0", "limit": 50}
        if damage == "malformed_message":
            with pytest.raises(ValueError):
                test_client.get(url, params=params)
            return
        response = test_client.get(url, params=params)

        if damage == "read_error":
            assert response.status_code == 503
            problem = response.json()
            assert problem["detail"] == f"Checkpoint history unavailable for agent {tid}"
            assert problem["status"] == 503
            assert problem["code"] == "http_503"
            assert problem["retryable"] is True
        else:
            assert response.status_code == 200
            assert response.json() == {"items": [], "msg_count": 3, "has_more": False}

    def test_long_conversation_keeps_standing_head_notes(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory"):
            messages.append(
                HumanMessage(
                    content=f"<system_note>\n{tag} note\n</system_note>",
                    additional_kwargs={
                        "meiva_msg_type": "system_note",
                        "meiva_note_tag": tag,
                        "meiva_created_at": "2026-08-27T00:00:00+00:00",
                    },
                )
            )
        for i in range(60):
            messages.append(
                HumanMessage(
                    content=f"user msg {i}",
                    additional_kwargs={
                        "meiva_msg_type": "inbound",
                        "meiva_created_at": f"2026-08-27T00:01:{i:02d}+00:00",
                        "meiva_source": "user",
                    },
                )
            )
        self._put_checkpoint(tid, messages, version="1")

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert [item["item_id"] for item in data["items"][:6]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
        ]
        assert [item["source"] for item in data["items"][1:6]] == [
            "exec_timeout",
            "timezone",
            "memory",
            "agent_id",
            "agent_memory",
        ]
        assert len(data["items"]) == 56
        assert data["items"][6]["item_id"] == "16.0"
        assert data["items"][-1]["item_id"] == "65.0"
        assert data["has_more"] is True

    def test_short_conversation_head_notes_not_duplicated(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory"):
            messages.append(
                HumanMessage(
                    content=f"<system_note>\n{tag} note\n</system_note>",
                    additional_kwargs={
                        "meiva_msg_type": "system_note",
                        "meiva_note_tag": tag,
                        "meiva_created_at": "2026-08-27T00:00:00+00:00",
                    },
                )
            )
        for i in range(5):
            messages.append(
                HumanMessage(
                    content=f"user msg {i}",
                    additional_kwargs={
                        "meiva_msg_type": "inbound",
                        "meiva_created_at": f"2026-08-27T00:01:{i:02d}+00:00",
                        "meiva_source": "user",
                    },
                )
            )
        self._put_checkpoint(tid, messages, version="1")

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert [item["item_id"] for item in data["items"]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
            "6.0",
            "7.0",
            "8.0",
            "9.0",
            "10.0",
        ]
        assert data["has_more"] is False

    def test_cursor_past_head_notes_crosses_to_older_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 1), version="1", boundary=True
        )
        notes = [
            HumanMessage(
                content=f"<system_note>\n{tag} note\n</system_note>",
                additional_kwargs={
                    "meiva_msg_type": "system_note",
                    "meiva_note_tag": tag,
                    "meiva_created_at": "2026-08-27T00:00:00+00:00",
                },
            )
            for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory")
        ]
        self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("current summary"),
                AIMessage(content="current item"),
            ],
            version="2",
        )
        reconfigure(test_client, timeline_compact_history=1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "7.0", "limit": 50}
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
            "6.0",
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert page["has_more"] is False

    def test_historical_head_note_cursor_crosses_to_next_older_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        boundary_2 = self._put_checkpoint(
            tid, self._segment("oldest", 1), version="1", boundary=True
        )
        notes = [
            HumanMessage(
                content=f"<system_note>\n{tag} note\n</system_note>",
                additional_kwargs={
                    "meiva_msg_type": "system_note",
                    "meiva_note_tag": tag,
                    "meiva_created_at": "2026-08-27T00:00:00+00:00",
                },
            )
            for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory")
        ]
        boundary_1 = self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("middle summary"),
                AIMessage(content="middle item"),
            ],
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("current summary"),
                AIMessage(content="current item"),
            ],
            version="3",
        )
        reconfigure(test_client, timeline_compact_history=-1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_1}.2.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            f"s1.{boundary_1}.0.0",
            f"s1.{boundary_1}.1.0",
            f"s2.{boundary_2}.0.0",
            f"s2.{boundary_2}.1.0",
        ]
        assert page["has_more"] is False
