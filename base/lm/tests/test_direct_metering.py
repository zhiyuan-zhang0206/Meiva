# meiva:note
# Direct `build_chat_model(...).invoke()` calls write one llm_usage row (task #5327). These
# tests drive the real factory through its documented MEIVA_LLM_OVERRIDE seam and assert on
# the emitted loguru event - the same record the event pipeline stores.
# meiva:link base/lm/docs/usage.meiva.okf.md

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    UsageMetadata,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from loguru import logger

from base.config.tests.configuration import model_overrides
from base.lm import metering
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model
from base.lm.usage import log_usage_from_message
from base.log.tests.log_records import LogRecords
from base.telemetry import process_state

_MODEL = "deepseek-flash"
_USAGE: UsageMetadata = {"input_tokens": 1_000, "output_tokens": 100, "total_tokens": 1_100}
_OVERRIDE_PATH = "tests._direct_metering_chat_model:build"


def _fresh(answer: AIMessage) -> AIMessage:
    return AIMessage(content=answer.content, usage_metadata=answer.usage_metadata)


class _DirectChatModel(BaseChatModel):
    answer: AIMessage
    streamed: bool = False

    @property
    def _llm_type(self) -> str:
        return "direct-metering-test"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=_fresh(self.answer))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        if self.streamed:
            yield ChatGenerationChunk(message=AIMessageChunk(content="ok", usage_metadata=_USAGE))


def _install_override_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("tests._direct_metering_chat_model")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def _build(
    monkeypatch: pytest.MonkeyPatch,
    catalog: ModelCatalog,
    *,
    meter: bool = True,
    streamed: bool = False,
) -> tuple[BaseChatModel, _DirectChatModel]:
    module = _install_override_module(monkeypatch)
    raw = _DirectChatModel(answer=AIMessage(content="ok", usage_metadata=_USAGE), streamed=streamed)

    def _override_build(_model: str, *, agent_id: int | None) -> _DirectChatModel:
        return raw

    module.__dict__["build"] = _override_build
    built = build_chat_model(
        _MODEL,
        catalog=catalog,
        llm_override=_OVERRIDE_PATH,
        overrides=model_overrides(),
        meter=meter,
    )
    return built, raw


def _usage_rows(records: LogRecords) -> list[dict[str, Any]]:
    return records.events("llm_usage")


def test_a_direct_call_writes_one_priced_usage_row(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)
    monkeypatch.setitem(process_state, "agent_id", 6949)

    llm.invoke([HumanMessage("hello")])

    [row] = _usage_rows(loguru_records)
    extra = row["extra"]
    assert extra["agent_id"] == 6949
    assert extra["attribution"] == "process"
    assert extra["usage_kind"] == metering.DIRECT_USAGE_KIND == "direct"
    assert extra["model"] == _MODEL
    assert (extra["in_total"], extra["out_total"]) == (1_000, 100)
    assert extra["calls"] == 1
    assert extra["cost_usd"] > 0


def test_a_call_without_process_identity_is_marked_unattributed(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)
    monkeypatch.setitem(process_state, "agent_id", None)

    with logger.contextualize(agent_id="-"):
        llm.invoke([HumanMessage("hello")])

    [row] = _usage_rows(loguru_records)
    assert row["extra"]["attribution"] == "unattributed"
    assert row["extra"]["agent_id"] == "-"


def test_a_caller_that_logs_the_same_response_does_not_double_count(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)

    response = llm.invoke([HumanMessage("hello")])
    log_usage_from_message(response, _MODEL, usage_kind="chat", catalog=model_catalog)

    assert len(_usage_rows(loguru_records)) == 1
    assert response.additional_kwargs["meiva_usage"]["model"] == _MODEL


def test_each_call_writes_its_own_row(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)

    llm.invoke([HumanMessage("first")])
    llm.invoke([HumanMessage("second")])

    assert len(_usage_rows(loguru_records)) == 2


def test_a_tool_bound_model_is_metered_through_the_binding(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)

    tool: dict[str, Any] = {
        "type": "function",
        "function": {"name": "execute_code", "parameters": {}},
    }
    llm.bind(tools=[tool]).invoke([HumanMessage("hello")])

    assert len(_usage_rows(loguru_records)) == 1


async def test_an_async_direct_call_is_metered(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)

    await llm.ainvoke([HumanMessage("hello")])

    [row] = _usage_rows(loguru_records)
    assert row["extra"]["in_total"] == 1_000


def test_meter_false_leaves_the_row_to_the_caller(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog, meter=False)

    llm.invoke([HumanMessage("hello")])

    assert _usage_rows(loguru_records) == []
    assert not llm.callbacks


def test_unmeter_strips_a_meter_from_an_injected_build(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog)

    assert metering.unmeter(llm) is llm
    llm.invoke([HumanMessage("hello")])

    assert _usage_rows(loguru_records) == []


def test_installing_the_meter_twice_still_writes_one_row(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, raw = _build(monkeypatch, model_catalog)

    metering.install_usage_meter(raw, model_name=_MODEL, catalog=model_catalog)
    llm.invoke([HumanMessage("hello")])

    assert len(_usage_rows(loguru_records)) == 1


def test_a_streamed_call_is_metered_once(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, _ = _build(monkeypatch, model_catalog, streamed=True)

    assert list(llm.stream([HumanMessage("hello")]))

    [row] = _usage_rows(loguru_records)
    assert row["extra"]["out_total"] == 100


def test_a_metering_failure_never_fails_the_call(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords, model_catalog: ModelCatalog
) -> None:
    llm, raw = _build(monkeypatch, model_catalog, meter=False)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("accounting broke")

    metering.install_usage_meter(raw, model_name=_MODEL, catalog=model_catalog, emit=_boom)

    response = llm.invoke([HumanMessage("hello")])

    assert response.content == "ok"
    assert _usage_rows(loguru_records) == []
    [failure] = loguru_records.events("llm_usage_metering_failed")
    assert failure["level"].name == "WARNING"
    assert failure["extra"]["model"] == _MODEL
    assert failure["extra"]["error_type"] == "RuntimeError"
    assert failure["extra"]["failures"] == 1
