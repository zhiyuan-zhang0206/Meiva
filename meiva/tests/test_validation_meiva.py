# meiva:note
# Argument validation of the `meiva.files`, `meiva.ui`, `meiva.web`, `meiva.understand` and `meiva.self` entry points.

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.agents.lifecycle import SystemHalt
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from meiva import files as _files
from meiva.tests.fixtures.pin_agent import pin_agent


class TestFilesEntries:
    def test_path_and_content_unwrap(self, tmp_path: Path) -> None:
        p = tmp_path / "x.txt"
        _files.write((str(p),), ("content",))  # pyright: ignore[reportArgumentType]
        assert _files.read((str(p),)) == "content"  # pyright: ignore[reportArgumentType]
        _files.append(str(p), ("more",))  # pyright: ignore[reportArgumentType]
        _files.edit(str(p), ("content",), ("CONTENT",))  # pyright: ignore[reportArgumentType]
        assert _files.read(str(p)) == "CONTENTmore"
        assert _files.glob((str(tmp_path / "*.txt"),)) == [p]  # pyright: ignore[reportArgumentType]
        _files.delete((str(p),))  # pyright: ignore[reportArgumentType]

    def test_path_objects_still_work(self, tmp_path: Path) -> None:
        """Zero regression: Path arguments keep working (str | Path params)."""
        p = tmp_path / "y.txt"
        _files.write(p, "ok")
        assert _files.read(p) == "ok"
        _files.delete(p)

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(
                lambda: _files.write(("a", "b"), "x"),  # pyright: ignore[reportArgumentType]
                "path must be a string",
                id="multi-path",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.write("x", ("a", "b")),  # pyright: ignore[reportArgumentType]
                "content must be a string",
                id="multi-content",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(lambda: _files.read(("a", "b")), "path must be a string", id="read-multi"),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.edit("x", ("a", "b"), "c"),  # pyright: ignore[reportArgumentType]
                "old must be a string",
                id="edit-old",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.delete(("a", "b")),  # pyright: ignore[reportArgumentType]
                "path must be a string",
                id="delete-multi",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.read("x", start=("1",)),  # pyright: ignore[reportArgumentType]
                "start must be int",
                id="read-start",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_multi_element_and_wrong_types_type_error(
        self, tmp_path: Path, call: Any, match: str
    ) -> None:
        p = tmp_path / "z.txt"
        _files.write(str(p), "x")  # ensure the read anchor exists
        with pytest.raises(TypeError, match=match):
            call()


class TestWebEntries:
    def test_fetch_effort_unwraps(self) -> None:
        from meiva.web import plans

        plan = plans.plan_fetch(
            [("https://example.com", "summarize")],
            50_000,
            ("low",),  # pyright: ignore[reportArgumentType]
            None,
        )
        assert plan.effort == "low"
        assert plan.max_chars == 50_000

    def test_fetch_effort_multi_element_type_errors(self) -> None:
        from meiva import web

        with pytest.raises(TypeError, match="effort must be a string"):
            web.fetch([("https://example.com", "summarize")], effort=("low", "high"))  # pyright: ignore[reportArgumentType]

    def test_search_count_never_unwraps(self) -> None:
        from meiva.web import plans

        with pytest.raises(TypeError, match="count must be int"):
            plans.plan_search(["q"], (5,), None)  # pyright: ignore[reportArgumentType]


class TestUnderstandEntries:
    def test_effort_unwraps(self) -> None:
        from meiva.understand import plans

        plan = plans.normalize(
            [{"prompt": "p", "text": "t"}],
            ("low",),  # pyright: ignore[reportArgumentType]
            None,
        )
        assert plan.effort == "low"

    def test_effort_multi_element_type_errors(self) -> None:
        from meiva.understand import understand

        with pytest.raises(TypeError, match="effort must be a string"):
            understand([{"prompt": "p", "text": "t"}], effort=("low", "high"))  # pyright: ignore[reportArgumentType, reportCallIssue]


class TestSelfEntries:
    def test_compact_summary_unwraps(
        self,
        db_url: str,
        redis_url: str,
        db_conn: psycopg.Connection[Any],
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
        event_bus: EventBus,
    ) -> None:
        """The summary tuple unwraps before the framework's boot guard runs —
        the DB write then receives the unwrapped string."""
        from meiva import self as self_mod

        agent_id = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        pin_agent(agent_id, bus=event_bus)
        with pytest.raises(SystemHalt):
            self_mod.compact(("summary",))  # pyright: ignore[reportArgumentType]
        row = db_conn.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_summary'",
            (agent_id,),
        ).fetchone()
        assert row == ("summary",)

    def test_compact_multi_element_type_errors(self) -> None:
        from meiva import self as self_mod

        with pytest.raises(TypeError, match="summary must be a string"):
            self_mod.compact(("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_pause_heartbeat_duration_never_unwraps(self) -> None:
        from meiva import self as self_mod

        with pytest.raises(TypeError, match="duration must be"):
            self_mod.pause_heartbeat(("1800",))  # pyright: ignore[reportArgumentType]
