from __future__ import annotations

import psycopg
import pytest

from base.config import settings
from base.db import create_agent
from gateway.tests.auth.gateway_config import GatewayTestClient
from gateway.tests.history.test_timeline import CompactHistoryCases
from gateway.tests.history.test_timeline import test_client as test_client
from gateway.tests.support.checkpoints.unreadable import put_unreadable_head


class TestTimelineCompactHistoryBoundaries(CompactHistoryCases):
    def test_initial_short_window_reports_history_only_when_enabled(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("history", 2), version="1", boundary=True)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )

        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = 0
        disabled = test_client.get(f"/api/agents/{tid}/timeline", params={"limit": 50}).json()
        assert disabled["has_more"] is False

        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = 1
        enabled = test_client.get(f"/api/agents/{tid}/timeline", params={"limit": 50}).json()
        assert enabled["has_more"] is True
        assert all(not item["item_id"].startswith("s") for item in enabled["items"])

    def test_pages_within_segment_then_reaches_its_summary(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 5), version="1", boundary=True
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )
        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = -1

        crossed = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "2.0", "limit": 2}
        ).json()
        assert [item["item_id"] for item in crossed["items"]] == [
            "0.0",
            "1.0",
            f"s1.{boundary_id}.4.0",
            f"s1.{boundary_id}.5.0",
        ]
        assert crossed["has_more"] is True

        middle = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.4.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in middle["items"]] == [
            f"s1.{boundary_id}.2.0",
            f"s1.{boundary_id}.3.0",
        ]
        assert middle["has_more"] is True

        head = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.2.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in head["items"]] == [
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert head["items"][0]["kind"] == "inbound_compact_summary"
        assert head["has_more"] is False

    def test_exact_tail_boundary_delivers_summary_while_crossing_segments(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(
            tid,
            self._segment("older", 1),
            version="1",
            boundary=True,
        )
        newer_id = self._put_checkpoint(
            tid,
            self._segment("newer", 50),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = -1

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{newer_id}.1.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s2.{older_id}.0.0",
            f"s2.{older_id}.1.0",
        ]
        assert page["items"][0]["kind"] == "inbound_compact_summary"
        assert page["has_more"] is False

    def test_depth_limit_still_delivers_oldest_allowed_segment_summary(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("blocked", 1), version="1", boundary=True)
        allowed_id = self._put_checkpoint(
            tid,
            self._segment("allowed", 50),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = 1

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{allowed_id}.1.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [f"s1.{allowed_id}.0.0"]
        assert page["items"][0]["kind"] == "inbound_compact_summary"
        assert page["has_more"] is False

    @pytest.mark.parametrize("depth", [-1, 1])
    def test_summary_only_segment_is_bounded_continuation_or_depth_terminal(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
        depth: int,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(
            tid,
            self._segment("older", 1),
            version="1",
            boundary=True,
        )
        summary_only_id = self._put_checkpoint(
            tid,
            self._segment("summary only", 0),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = depth

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": "2.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            f"s1.{summary_only_id}.0.0",
        ]
        assert page["has_more"] is (depth == -1)
        if depth == -1:
            continued = test_client.get(
                f"/api/agents/{tid}/timeline",
                params={"before": f"s1.{summary_only_id}.0.0", "limit": 50},
            ).json()
            assert [item["item_id"] for item in continued["items"]] == [
                f"s2.{older_id}.0.0",
                f"s2.{older_id}.1.0",
            ]
            assert continued["has_more"] is False

    def test_depth_and_checkpoint_id_control_cross_segment_access(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(tid, self._segment("older", 2), version="1", boundary=True)
        newer_id = self._put_checkpoint(tid, self._segment("newer", 2), version="2", boundary=True)
        current_id = self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )

        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = 1
        blocked = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s2.{older_id}.1.0", "limit": 50},
        ).json()
        assert blocked == {"items": [], "msg_count": 3, "has_more": False}

        non_boundary = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{current_id}.1.0", "limit": 50},
        ).json()
        assert non_boundary == {"items": [], "msg_count": 3, "has_more": False}

        invalid_rank = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s0.{newer_id}.1.0", "limit": 50},
        ).json()
        assert invalid_rank == {"items": [], "msg_count": 3, "has_more": False}

        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = -1
        stale_rank = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s99.{newer_id}.2.0", "limit": 1},
        ).json()
        assert [item["item_id"] for item in stale_rank["items"]] == [f"s1.{newer_id}.1.0"]

        head = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s99.{newer_id}.2.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in head["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s1.{newer_id}.1.0",
        ]
        assert head["has_more"] is True

        crossed = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{newer_id}.1.0", "limit": 50},
        ).json()
        assert [item["item_id"] for item in crossed["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s2.{older_id}.0.0",
            f"s2.{older_id}.1.0",
            f"s2.{older_id}.2.0",
        ]
        assert crossed["has_more"] is False

    def test_historical_page_does_not_deserialize_the_current_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 2), version="1", boundary=True
        )
        # meiva:note
        # The live head declares three messages and cannot be deserialized: a page that
        # reads it would be a 503, so a 200 with the exact count proves it was not read.
        put_unreadable_head(settings.data_plane.db_url, tid, messages=3, version="2")
        test_client.app.state.config_authority.runtime.gateway.timeline_compact_history = 1

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.2.0", "limit": 1},
        )

        assert page.status_code == 200
        assert page.json()["msg_count"] == 3
        assert [item["item_id"] for item in page.json()["items"]] == [f"s1.{boundary_id}.1.0"]
