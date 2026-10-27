"""Pre-compact history dump tests (`agent/hooks/history_dump.py`).

Covers the four contracts of the feature:
- config gating: on (the declared default) → dump written + note injected; off
  → no file, no note (the suite's autouse fixture keeps it off for every test
  outside this file);
- filename: `<start>__<end>.jsonl` — start = the earliest message
  `meiva_created_at` (falls back to the write moment), end = the write moment;
- dump content completeness + replayability: one JSONL line per message, each
  line a LangChain BaseMessage `model_dump(mode="json")` that round-trips
  through `langchain_core.load.loads` with type / content / kwargs intact;
- rotation: only the newest `history_dump_keep` dumps survive;
- note injection position: the note rides the parked `context_reset.tail`
  AFTER the summary — never the live `messages` channel, and never between an
  AIMessage and its ToolMessage (the DeepSeek anthropic endpoint rejects that
  shape with a 400). Both compact paths (auto hook + claim node) are covered.
"""

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.hooks.compact import auto_compact_for_llm, compose_summary_message
from agent.hooks import history_dump
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.tests.units import spawn_agent
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.domains.agent.settings import AgentCompactionSettings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices, HistoryDump
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.paths import workspace_dir

# ── helpers ──


def _instant(tick: int = 1) -> datetime:
    return datetime(2026, 8, 13, 12, 0, 0, tick, tzinfo=UTC)


def _dump(
    messages: list[AnyMessage],
    workspace: Path,
    *,
    enabled: bool = True,
    keep: int = 5,
    tick: int = 1,
) -> Any:
    return history_dump.dump_history(
        messages,
        workspace,
        HistoryDump(history_dump_enabled=enabled, history_dump_keep=keep),
        now=_instant(tick),
    )


def _clear(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


# meiva:note
# The Meiva home of these tests: a directory of their own, so the context the compaction runs in and the
# workspace the dump lands in name the same home without it being the session's.
@pytest.fixture
def meiva_test_home(tmp_path: Path) -> Path:
    home = tmp_path / "meiva-home"
    home.mkdir()
    return home


# meiva:note
# The workspace of agent 1 under that home, with no dumps in it: the auto-compact tests run as agent 1 and write where the production code writes.
@pytest.fixture
def agent_workspace(meiva_test_home: Path) -> Path:
    workspace = workspace_dir(1, home=meiva_test_home)
    _clear(workspace / "message-history")
    return workspace


# `<start>__<end>.jsonl`, both UTC: start with second precision, end with
# microsecond precision (the collision guard).
_DUMP_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)__(\d{8}T\d{6}\.\d{6}Z)\.jsonl$")


def _parse_dump_name(path: Path) -> tuple[datetime, datetime]:
    """The (start, end) stamps a dump filename carries."""
    m = _DUMP_NAME_RE.match(path.name)
    assert m is not None, path.name
    start = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    end = datetime.strptime(m.group(2), "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    return start, end


def _sample_messages() -> list[AnyMessage]:
    """A representative conversation: system prompt, inbound, tool round-trip,
    an AI reply, and a framework system note — every shape the dump must keep."""
    return [
        SystemMessage(content="<sys prompt>"),
        HumanMessage(
            content="User hello",
            additional_kwargs={"meiva_msg_type": "inbound", "meiva_source": "user"},
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}],
            usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
        ),
        ToolMessage(
            content="ran ok",
            tool_call_id="c1",
            additional_kwargs={"meiva_msg_type": "exec_output"},
        ),
        AIMessage(content="done"),
        system_note_message(
            content="a framework note",
            tag=NoteTag.COMPACT_REMINDER,
            created_at=datetime.now(UTC),
        ),
    ]


def _compact_tail(update: Any) -> list[AnyMessage]:
    """Same transport assertion as agent/tests/test_compact.py: the window is
    cleared (REMOVE_ALL sentinel alone) and what follows the head is parked in
    `context_reset.tail`. Returns the tail."""
    msgs = update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], RemoveMessage)
    assert msgs[0].id == REMOVE_ALL_MESSAGES
    return update["context_reset"].tail


def _fake_llm(summary_text: str) -> Any:
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content=summary_text))
    return llm


_LONG_SUMMARY = "## Requests\nfollow the template. " * 60


def _runtime_with_llm(
    llm: Any, database_gate: ProcessDbGate, *, home: Path, enabled: bool = True
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            meiva_home=home,
            user_home=home.parent,
            ops_pool=None,
            llm=llm,
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                {
                    "history_dump_enabled": enabled,
                    "history_dump_keep": 5,
                    "auto_compact_fraction": 1e-6,
                    "compact_reminder_fraction": 1e-6,
                },
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
            ),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng()),
            catalog=build_model_catalog(home=home),
            clock_factory=Clock.from_settings,
        )
    )


def _fake_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "1"}}


def _over_threshold_state() -> AgentState:
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 1000) for _ in range(5)),
        ],
        halted=False,
    )


def _make_runtime(
    *,
    ops_pool: AsyncConnectionPool | None = None,
    llm: Any | None = None,
    database_gate: ProcessDbGate,
    home: Path,
    enabled: bool = True,
) -> Runtime[MeivaContext]:
    ctx = MeivaContext(
        meiva_home=home,
        user_home=home.parent,
        ops_pool=ops_pool,
        llm=llm if llm is not None else _fake_llm("synthetic summary"),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            {"history_dump_enabled": enabled, "history_dump_keep": 5},
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
        ),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng()),
        catalog=build_model_catalog(home=home),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def _insert_inbound_kind(
    db: psycopg.Connection, tid: int, content: str, kind: str, source: str = "system"
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tid, content, kind, source),
        )
        new_id = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return new_id


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}


# ── dump_history: config gating ──


def test_dump_enabled_by_default() -> None:
    """The feature is on by default (the Task #1249 opt-in flipped, 2026-09-11).
    The suite's autouse fixture disables it per test, so assert the field's
    *declared* default — a silent flip back must fail here."""
    assert AgentCompactionSettings.model_fields["history_dump_enabled"].default is True


def test_dump_disabled_returns_none(tmp_path: Path):
    """Config off → dump_history returns None and creates nothing."""
    ws = tmp_path / "workspace"
    assert _dump(_sample_messages(), ws, enabled=False) is None
    assert not (ws / "message-history").exists()


def test_dump_enabled_writes_file(tmp_path: Path):
    """Config on → the dump is written under <workspace>/message-history/ and
    the path is returned."""
    ws = tmp_path / "workspace"
    path = _dump(_sample_messages(), ws)
    assert path is not None
    assert path.parent == ws / "message-history"
    assert path.name.endswith(".jsonl")
    assert path.exists()


# ── dump_history: filename = round start + compaction moment ──


# meiva:note
# The name stamps the round: start = the earliest message `meiva_created_at`, end = the write moment the caller passes.
def test_dump_filename_is_start_and_end(tmp_path: Path):
    messages: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        HumanMessage(
            content="later",
            additional_kwargs={"meiva_created_at": "2026-08-13T09:30:00+00:00"},
        ),
        HumanMessage(
            content="earlier",
            additional_kwargs={"meiva_created_at": "2026-08-13T08:15:00+00:00"},
        ),
    ]
    path = _dump(messages, tmp_path / "workspace")
    assert path is not None
    start, end = _parse_dump_name(path)
    assert start == datetime(2026, 8, 13, 8, 15, tzinfo=UTC)
    assert end == _instant(1)


def test_dump_filename_falls_back_to_write_moment(tmp_path: Path):
    """A stamp-less (or unparseable) history degrades to start == end — the name
    stays well-formed and every dump remains parseable/sortable."""
    messages: list[AnyMessage] = [
        HumanMessage(content="no stamp", additional_kwargs={"meiva_created_at": "not-a-date"}),
        SystemMessage(content="<sys>"),
    ]
    path = _dump(messages, tmp_path / "workspace")
    assert path is not None
    start, end = _parse_dump_name(path)
    assert start == end.replace(microsecond=0)


# ── dump_history: content completeness + replayability ──


def test_dump_is_replayable_jsonl(tmp_path: Path):
    """One JSON line per message, each line a raw BaseMessage model_dump that
    round-trips through langchain load — type, content, and meiva_* kwargs all
    survive, SystemMessage and ToolMessages included (full-history snapshot)."""
    messages = _sample_messages()
    path = _dump(messages, tmp_path / "workspace")
    assert path is not None

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(messages)

    replayed: list[BaseMessage] = []
    for line in lines:
        raw = json.loads(line)
        assert isinstance(raw, dict)
        # The documented replay recipe: wrap the raw fields in the langchain
        # type/data envelope and let messages_from_dict rebuild the message.
        replayed.extend(
            messages_from_dict(
                [
                    {
                        "type": raw["type"],
                        "data": {k: v for k, v in raw.items() if k != "type"},
                    }
                ]
            )
        )

    assert [type(m).__name__ for m in replayed] == [type(m).__name__ for m in messages]
    for original, restored in zip(messages, replayed, strict=True):
        assert restored.content == original.content  # pyright: ignore[reportUnknownMemberType]
        assert restored.additional_kwargs == original.additional_kwargs  # pyright: ignore[reportUnknownMemberType]
    # The tool round-trip survived: tool_call ids match their ToolMessage.
    tool_msg = replayed[3]
    assert isinstance(tool_msg, ToolMessage)
    assert tool_msg.tool_call_id == "c1"


# meiva:note
# After each write only the newest `history_dump_keep` dumps remain — disk growth is bounded (each dump is a full conversation snapshot). The messages carry no stamps, so each dump's start is its own write moment.
def test_dump_rotates_to_newest_keep(tmp_path: Path):
    messages: list[AnyMessage] = [HumanMessage(content="old")]
    for tick in range(1, 5):
        _dump(messages, tmp_path / "workspace", keep=2, tick=tick)
    dump_dir = tmp_path / "workspace" / "message-history"
    dumps = sorted(p.name for p in dump_dir.glob("*.jsonl"))
    assert len(dumps) == 2
    assert dumps == sorted(dumps)  # lexicographic == chronological
    assert dumps[-1].endswith("20260813T120000.000004Z.jsonl")  # the newest write


# meiva:note
# A write failure must never raise — the dump is forensics, the compaction is the critical path. Returns None and the caller injects no note. The fault is real: the dump directory's place is taken by a file.
def test_dump_failure_is_best_effort(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "message-history").write_text("a file, not a directory")
    assert _dump(_sample_messages(), workspace) is None


# ── auto-compact path: note injection position ──


async def test_auto_compact_injects_dump_note_after_summary(
    meiva_test_home: Path, agent_workspace: Path, database_gate: ProcessDbGate
):
    """Enabled → the auto-compact hook dumps the pre-compact history and parks
    the note in the fresh-context tail AFTER the summary. The live `messages`
    update carries only the REMOVE_ALL sentinel — the note is never in the
    pre-compact channel and never between an AIMessage and its ToolMessage."""
    ws = agent_workspace
    state = _over_threshold_state()

    result = await auto_compact_for_llm(
        state,
        _runtime_with_llm(
            _fake_llm(_LONG_SUMMARY), database_gate=database_gate, home=meiva_test_home
        ),
        _fake_config(),
    )
    assert result is not None

    tail = _compact_tail(result)
    assert len(tail) == 2
    assert tail[0].content == compose_summary_message(_LONG_SUMMARY)  # pyright: ignore[reportUnknownMemberType]
    note = tail[1]
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["meiva_note_tag"] == "history_dump"  # pyright: ignore[reportUnknownMemberType]
    # The note names the real, existing dump and advertises the grep recipe.
    (dump_path,) = (ws / "message-history").glob("*.jsonl")
    assert dump_path.name in note.content  # pyright: ignore[reportUnknownMemberType]
    assert "grep" in str(note.content).lower()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    assert dump_path.exists()


async def test_auto_compact_no_note_when_disabled(
    meiva_test_home: Path, agent_workspace: Path, database_gate: ProcessDbGate
):
    """Config off → the auto-compact transition is exactly the old shape:
    tail = the summary alone, no note (and no dump file)."""
    state = _over_threshold_state()

    result = await auto_compact_for_llm(
        state,
        _runtime_with_llm(
            _fake_llm(_LONG_SUMMARY),
            database_gate=database_gate,
            home=meiva_test_home,
            enabled=False,
        ),
        _fake_config(),
    )
    assert result is not None
    tail = _compact_tail(result)
    assert [m.content for m in tail] == [compose_summary_message(_LONG_SUMMARY)]  # pyright: ignore[reportUnknownMemberType]
    assert not (agent_workspace / "message-history").exists()


# meiva:note
# A dump failure degrades to today's behavior: compaction still applies, no note is injected. The fault is real: the dump directory's place is taken by a file.
async def test_auto_compact_proceeds_when_dump_fails(
    meiva_test_home: Path, agent_workspace: Path, database_gate: ProcessDbGate
):
    agent_workspace.mkdir(parents=True, exist_ok=True)
    (agent_workspace / "message-history").write_text("a file, not a directory")
    state = _over_threshold_state()

    result = await auto_compact_for_llm(
        state,
        _runtime_with_llm(
            _fake_llm(_LONG_SUMMARY), database_gate=database_gate, home=meiva_test_home
        ),
        _fake_config(),
    )
    assert result is not None
    tail = _compact_tail(result)
    assert [m.content for m in tail] == [compose_summary_message(_LONG_SUMMARY)]  # pyright: ignore[reportUnknownMemberType]


# ── claim path (agent-/user-triggered compact): note injection position ──


async def test_claim_compact_summary_parks_dump_note_after_summary(
    meiva_test_home: Path,
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """Enabled → the claim-node compact path (agent-written summary) dumps the
    pre-compact state.messages and parks the note after the summary in the
    fresh-context tail. The messages update is the REMOVE_ALL sentinel alone:
    no note, no AIMessage — nothing that could sit between a tool_use and its
    tool_result on the wire."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _insert_inbound_kind(db_conn, tid, "agent-written summary", "compact_summary")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"old-{i}") for i in range(8)),
        ],
        halted=False,
    )

    cmd = await claim_node(
        state,
        _make_runtime(
            home=meiva_test_home,
            ops_pool=aops_pool,
            llm=_fake_llm("LLM should not be called"),
            database_gate=database_gate,
        ),
        _config(tid),
    )

    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("agent-written summary")  # pyright: ignore[reportUnknownMemberType]
    assert len(tail) == 2  # summary + dump note, nothing else
    note = tail[-1]
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["meiva_note_tag"] == "history_dump"  # pyright: ignore[reportUnknownMemberType]
    # Fresh-context laydown (init_context): [SystemMessage, *notes, *tail] —
    # the note's neighbors are the summary and the next LLM output, never a
    # tool_use/tool_result pair.
    for m in tail:
        assert not (isinstance(m, AIMessage) and m.tool_calls)


async def test_claim_compact_summary_no_note_when_disabled(
    meiva_test_home: Path,
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """Config off → claim-path compact tail is exactly today's (summary only)."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _insert_inbound_kind(db_conn, tid, "agent-written summary", "compact_summary")
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="old")],
        halted=False,
    )

    cmd = await claim_node(
        state,
        _make_runtime(
            home=meiva_test_home,
            ops_pool=aops_pool,
            llm=_fake_llm("LLM should not be called"),
            database_gate=database_gate,
            enabled=False,
        ),
        _config(tid),
    )

    tail = _compact_tail(cmd.update)
    assert [m.content for m in tail] == [  # pyright: ignore[reportUnknownMemberType]
        compose_summary_message("agent-written summary")
    ]
