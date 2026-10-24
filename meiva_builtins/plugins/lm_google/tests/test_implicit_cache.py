from typing import Any

import pytest
from google.genai import types
from google.genai.errors import ClientError
from langchain_core.exceptions import ModelError
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.llm.invoke import ainvoke_tool_call
from base.config import settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.factory import build_chat_model
from base.lm.usage import log_usage_from_message
from meiva_builtins.plugins.lm_google.tests.gemini_server import GeminiServer


def _gemini_flash(model_catalog: ModelCatalog, server: GeminiServer) -> ChatGoogleGenerativeAI:
    llm = build_chat_model(
        "gemini-3.8-flash",
        catalog=model_catalog,
        keys=ProviderKeys({"GEMINI_API_KEY": "test-not-a-real-key"}),
        llm_override="",
        overrides=ModelOverrides.from_pins(None),
        streaming=False,
        base_url=server.url,
    )
    assert isinstance(llm, ChatGoogleGenerativeAI)
    return llm


async def test_google_tool_call_uses_complete_prefix_and_accounts_cache_reads(
    model_catalog: ModelCatalog,
    loguru_records: list[dict[str, Any]],
    gemini_server: GeminiServer,
) -> None:
    llm = _gemini_flash(model_catalog, gemini_server)
    gemini_server.answer(
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text="summary")]),
                    finish_reason=types.FinishReason.STOP,
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1000,
                candidates_token_count=100,
                total_token_count=1100,
                cached_content_token_count=200,
            ),
        )
    )
    response = await ainvoke_tool_call(
        llm,
        [SystemMessage(content="stable system head"), HumanMessage(content="history")],
        timeout_seconds=settings.lm.llm_compact_timeout_seconds,
    )
    assert response.text == "summary"
    [request] = gemini_server.received
    assert request.path == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert "cachedContent" not in request.body
    assert request.body["systemInstruction"]["parts"][0]["text"] == "stable system head"
    assert request.body["tools"][0]["functionDeclarations"][0]["name"] == "execute_code"
    assert request.body["contents"][0]["parts"][0]["text"] == "history"
    assert response.usage_metadata is not None
    assert response.usage_metadata.get("input_token_details", {}).get("cache_read") == 200

    log_usage_from_message(response, "gemini-3.8-flash", catalog=model_catalog)
    usage = next(r["extra"] for r in loguru_records if r["extra"].get("event") == "llm_usage")
    assert usage["cache_read"] == 200


async def test_cache_403_has_no_application_recovery(
    model_catalog: ModelCatalog, gemini_server: GeminiServer
) -> None:
    llm = _gemini_flash(model_catalog, gemini_server)
    gemini_server.refuse(403, "CachedContent not found or permission denied", "PERMISSION_DENIED")
    with pytest.raises(ModelError) as caught:
        await ainvoke_tool_call(
            llm,
            [SystemMessage(content="head"), HumanMessage(content="history")],
            timeout_seconds=settings.lm.llm_compact_timeout_seconds,
        )
    assert isinstance(caught.value.__cause__, ClientError)
    assert caught.value.__cause__.code == 403
    assert len(gemini_server.received) == 1
