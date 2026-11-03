from __future__ import annotations

import json
import time

import anthropic
import httpx2
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig

from agent.graph.llm._backoff import Attempt
from agent.graph.llm._chunk import assemble_final_message, validate_stop_reason
from agent.graph.llm.node import llm_attempt
from agent.graph.llm.tests.llm_support import (
    ScriptedModel,
    llm_runtime,
    published_payloads,
)
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    LLMRetryExhaustedError,
    LLMStreamCorruptedError,
    LLMStreamError,
    LLMStreamTruncatedError,
    LLMStreamUnexpectedStopReasonError,
    retry_exhausted_error,
)
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


def test_validate_passes_on_end_turn() -> None:
    msg = AIMessage(
        content="ok", response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"}
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_max_tokens() -> None:
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"}
    )
    with pytest.raises(LLMStreamTruncatedError, match=r"max_tokens.*output_tokens=0") as exc_info:
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))
    assert exc_info.value.stop_reason == "max_tokens"
    assert exc_info.value.output_tokens == 0


def test_validate_raises_includes_output_tokens_for_diagnosis() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 4096, "total_tokens": 5096},
    )
    with pytest.raises(LLMStreamTruncatedError, match=r"output_tokens=4096"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_missing_stop_reason() -> None:
    msg_empty = AIMessage(content="ok", response_metadata={"model_provider": "anthropic"})
    with pytest.raises(LLMStreamCorruptedError):
        validate_stop_reason(msg_empty, catalog=build_model_catalog(home=meiva_home()))
    msg_missing = AIMessage(
        content="ok", response_metadata={"model_provider": "anthropic", "stop_reason": None}
    )
    with pytest.raises(LLMStreamCorruptedError):
        validate_stop_reason(msg_missing, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_pause_turn() -> None:
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "pause_turn"}
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"pause_turn"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_passes_on_refusal() -> None:
    msg = AIMessage(
        content="I'm sorry, I can't help with that.",
        response_metadata={"model_provider": "anthropic", "stop_reason": "refusal"},
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_tool_use_without_tool_calls() -> None:
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"}
    )
    with pytest.raises(LLMStreamCorruptedError, match=r"tool_use.*tool_calls"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_openai_tool_calls_without_tool_calls() -> None:
    msg = AIMessage(
        content="", response_metadata={"model_provider": "openai", "finish_reason": "tool_calls"}
    )
    with pytest.raises(LLMStreamCorruptedError, match=r"tool_calls.*empty"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_passes_on_openai_tool_calls_with_tool_calls() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "openai", "finish_reason": "tool_calls"},
        tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "call_1"}],
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_passes_on_tool_use_with_tool_calls() -> None:
    msg = AIMessage(
        content=[],
        response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
        tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "call_1"}],
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_stop_sequence() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "stop_sequence"},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"stop_sequence"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_model_context_window_exceeded() -> None:
    msg = AIMessage(
        content="",
        response_metadata={
            "model_provider": "anthropic",
            "stop_reason": "model_context_window_exceeded",
        },
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"model_context_window_exceeded"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_validate_raises_on_unknown_stop_reason() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "future_value_xyz"},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"future_value_xyz"):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_gemini_stop_passes() -> None:
    msg = AIMessage(
        content="hello",
        response_metadata={"model_provider": "google_genai", "finish_reason": "STOP"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_openai_stop_passes() -> None:
    msg = AIMessage(
        content="hello",
        response_metadata={"model_provider": "openai", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_anthropic_end_turn_passes() -> None:
    msg = AIMessage(
        content="hello",
        response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_gemini_max_tokens_truncated() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "google_genai", "finish_reason": "MAX_TOKENS"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(LLMStreamTruncatedError):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_gemini_missing_corrupted() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "google_genai"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(LLMStreamCorruptedError):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_gemini_safety_unexpected() -> None:
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "google_genai", "finish_reason": "SAFETY"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


async def test_llm_node_validator_wired(
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    truncated = AIMessageChunk(
        content="thinking...",
        response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"},
        usage_metadata={"input_tokens": 10, "output_tokens": 4096, "total_tokens": 4106},
    )
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    async with published_payloads(event_bus) as (publisher, payloads):
        runtime = llm_runtime(
            ScriptedModel(scripts=[[truncated]]),
            model_catalog=model_catalog,
            database_gate=database_gate,
            event_publisher=publisher,
        )
        with pytest.raises(LLMStreamTruncatedError):
            await llm_attempt(state, runtime, config, Attempt(1, time.time()))

    roles = [json.loads(payload)["role"] for payload in payloads]
    assert "error" not in roles, (
        "llm_node should NOT emit Error event (would spam during langgraph retries)"
    )
    assert roles, "the node streamed its partial output to the live view before it raised"


def test_exception_hierarchy() -> None:
    assert issubclass(LLMStreamUnexpectedStopReasonError, FatalLLMStreamError)
    assert issubclass(LLMStreamUnexpectedStopReasonError, LLMStreamError)
    assert issubclass(LLMStreamCorruptedError, FatalLLMStreamError)
    assert issubclass(LLMStreamCorruptedError, LLMStreamError)
    assert issubclass(LLMStreamTruncatedError, LLMStreamUnexpectedStopReasonError)
    assert issubclass(LLMStreamTruncatedError, FatalLLMStreamError)
    assert issubclass(LLMRetryExhaustedError, FatalLLMStreamError)
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"}
    )
    with pytest.raises(LLMStreamError):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))
    with pytest.raises(FatalLLMStreamError):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))
    with pytest.raises(LLMStreamUnexpectedStopReasonError):
        validate_stop_reason(msg, catalog=build_model_catalog(home=meiva_home()))


def test_assemble_final_message_eof_drop_without_stop_reason_or_usage_raises_corrupted() -> None:
    chunks = [
        AIMessageChunk(
            content="partial output",
            response_metadata={"model_provider": "anthropic"},
        )
    ]
    with pytest.raises(LLMStreamCorruptedError, match=r"terminal reason missing") as exc_info:
        assemble_final_message(chunks, catalog=build_model_catalog(home=meiva_home()))
    assert isinstance(exc_info.value, FatalLLMStreamError)


def test_assemble_final_message_end_turn_without_usage_metadata_raises_corrupted() -> None:
    chunks = [
        AIMessageChunk(
            content="hello",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
        )
    ]
    with pytest.raises(
        LLMStreamCorruptedError,
        match=r"final_msg\.usage_metadata is None.*additional_kwargs=.*response_metadata=.*content_len=5.*tool_calls=0",
    ) as exc_info:
        assemble_final_message(chunks, catalog=build_model_catalog(home=meiva_home()))
    assert isinstance(exc_info.value, FatalLLMStreamError)


def test_retry_exhausted_error_preserves_transient_503_classification_and_cause() -> None:
    response = httpx2.Response(503, request=httpx2.Request("POST", "https://audit.invalid"))
    raw = anthropic.InternalServerError(
        "Service unavailable",
        response=response,
        body={
            "type": "error",
            "error": {"type": "service_unavailable_error", "message": "Service unavailable"},
        },
    )
    wrapped = retry_exhausted_error(raw)
    assert isinstance(wrapped, LLMRetryExhaustedError)
    assert isinstance(wrapped, FatalLLMStreamError)
    assert wrapped.error_class == "transient"
    assert wrapped.provider == "anthropic"
    assert wrapped.status == 503
    assert wrapped.__cause__ is raw
