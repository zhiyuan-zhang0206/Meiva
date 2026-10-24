# meiva:note
# `generate_label_async`: one label from one model answer, written once and announced once.
#
# The announcement is read back from the test's Redis by a real subscriber, the billing report is
# the `llm_usage` log record the usage accounting writes, and the label is the `agents` row.

from __future__ import annotations

from typing import Any

import httpx2
import openai
import psycopg
import pytest
from langchain_core.messages import AIMessage

from base.config.service_read import ConfigAuthority
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import recorded_events
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords
from base.packages.tests.model_catalog import inert_keys
from services.derived.labeler.labeler import generate_label_async
from services.derived.labeler.labeler import normalize as _normalize_to
from services.derived.labeler.tests.slices import labeler_config, labeler_db


def _normalize(raw: str) -> str:
    return _normalize_to(raw, labeler_config().labeler_max_chars)


def _label_of(conn: psycopg.Connection, agent_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


# meiva:note
# A chat model that answers every call with `content`.
class _ScriptedModel:
    def __init__(self, content: str | list[str | dict[str, Any]]) -> None:
        self.content = content

    async def ainvoke(self, _messages: list[Any]) -> AIMessage:
        return AIMessage(content=self.content)


# meiva:note
# A chat model whose provider connection fails.
class _UnreachableModel:
    async def ainvoke(self, _messages: Any) -> Any:
        raise openai.APIConnectionError(request=httpx2.Request("POST", "https://audit.invalid"))


def _fake_builder(llm: Any) -> Any:
    def _build(_model: str, **_kwargs: object) -> Any:
        return llm

    return _build


# meiva:note
# Generate the label of agent `tid` from `llm`'s answer; return the `label_updated` events that
# reached the events channel meanwhile.
async def _generate(
    tid: int,
    prompt: str,
    llm: Any,
    *,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> list[dict[str, Any]]:
    with recorded_events(event_bus) as events:
        await generate_label_async(
            tid,
            prompt,
            labeler_config(labeler_model="deepseek-v4-pro"),
            labeler_db(database_gate=database_gate),
            event_bus,
            build_chat_model=_fake_builder(llm),
            catalog=model_catalog,
            keys=inert_keys(model_catalog),
            llm_override=config_authority.runtime.lm.llm_override,
            overrides=ModelOverrides.from_pins({}),
        )
    return [event for event in events if event["role"] == "label_updated"]


class TestNormalize:
    def test_strip_quotes_and_truncate(self) -> None:
        assert _normalize('  "abc"  ') == "abc"

    def test_chinese_quote_brackets(self) -> None:
        assert _normalize("\u300c\u6d4b\u8bd5\u4efb\u52a1\u300d") == "\u6d4b\u8bd5\u4efb\u52a1"

    def test_truncate_at_max_chars(self) -> None:
        assert (
            _normalize("\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341")
            == "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341"
        )

    def test_first_line_only(self) -> None:
        assert _normalize("first line\nsecond line") == "first line"

    def test_empty(self) -> None:
        assert _normalize("   ") == ""


# meiva:note
# A successful model call is reported as one `batch` usage of the model that answered, for the
# agent the label is for. The message carries token usage but no label text, so no label is
# written.
@pytest.mark.asyncio
async def test_labeler_emits_batch_billing_after_a_successful_llm_call(
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    class _ResponseLLM:
        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return AIMessage(
                content="",
                usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )

    assert (
        await generate_label_async(
            1,
            "prompt",
            labeler_config(labeler_model="deepseek-v4-pro"),
            labeler_db(database_gate=database_gate),
            event_bus,
            build_chat_model=_fake_builder(_ResponseLLM()),
            catalog=model_catalog,
            keys=inert_keys(model_catalog),
            llm_override=config_authority.runtime.lm.llm_override,
            overrides=ModelOverrides.from_pins({}),
        )
        is False
    )
    [usage] = loguru_records.events("llm_usage")
    extra = usage["extra"]
    assert (extra["model"], extra["usage_kind"], extra["agent_id"]) == (
        "deepseek-v4-pro",
        "batch",
        1,
    )
    assert (extra["in_total"], extra["out_total"]) == (10, 2)


class TestGenerateLabelAsync:
    @pytest.mark.asyncio
    async def test_writes_label_when_null_and_publishes(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)

        published = await _generate(
            tid,
            "\u67e5\u4e00\u4e0b X \u6a21\u5757\u600e\u4e48\u8c03",
            _ScriptedModel("\u67e5 X \u6a21\u5757"),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) == "\u67e5 X \u6a21\u5757"
        assert db_conn.execute(
            "SELECT label_user_set FROM agents WHERE id = %s", (tid,)
        ).fetchone() == (False,)
        assert [(event["agent_id"], event["label"]) for event in published] == [
            (tid, "\u67e5 X \u6a21\u5757")
        ]

    @pytest.mark.asyncio
    async def test_cas_skips_when_label_already_set(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET label='\u7528\u6237\u6539\u7684', label_user_set=TRUE WHERE id=%s",
                (tid,),
            )
        db_conn.commit()

        published = await _generate(
            tid,
            "\u539f\u59cb prompt",
            _ScriptedModel("LLM \u8d77\u7684"),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) == "\u7528\u6237\u6539\u7684"
        assert published == []

    @pytest.mark.asyncio
    async def test_cas_skips_after_user_reset(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET label=NULL, label_user_set=TRUE WHERE id=%s",
                (tid,),
            )
        db_conn.commit()

        published = await _generate(
            tid,
            "\u539f\u59cb prompt",
            _ScriptedModel("LLM \u8d77\u7684"),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_llm_failure_leaves_label_null(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)

        published = await _generate(
            tid,
            "p",
            _UnreachableModel(),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_empty_normalized_label_skipped(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)

        published = await _generate(
            tid,
            "p",
            _ScriptedModel('"   "'),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_extracts_text_from_thinking_blocks(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)

        published = await _generate(
            tid,
            "migrate database schema",
            _ScriptedModel(
                [
                    {
                        "type": "thinking",
                        "thinking": "thinking...",
                        "signature": "sig-abc123",
                        "index": 0,
                    },
                    {"type": "text", "text": "migrate data"},
                ]
            ),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) == "migrate data"
        assert [event["label"] for event in published] == ["migrate data"]

    @pytest.mark.asyncio
    async def test_thinking_only_blocks_yield_empty(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)

        published = await _generate(
            tid,
            "p",
            _ScriptedModel(
                [
                    {
                        "type": "thinking",
                        "thinking": "analyzing...",
                        "signature": "sig-xyz",
                        "index": 0,
                    }
                ]
            ),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_joined(
        self,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        tid = create_agent(db_conn)

        await _generate(
            tid,
            "p",
            _ScriptedModel(
                [
                    {"type": "text", "text": "migrate"},
                    {"type": "thinking", "thinking": "hmm", "signature": "sig", "index": 0},
                    {"type": "text", "text": "data"},
                ]
            ),
            event_bus=event_bus,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _label_of(db_conn, tid) == "migrate data"
