# meiva:note
# `langchain-google-genai` returns a Gemini-3+ response as content blocks: a
# thought part becomes {"type": "thinking", "thinking": ...}, a text part
# {"type": "text", "text": ...}, and a function call a normalized tool call.
# These hand-built fixtures (no live API call) pin that a real turn keeps its
# text and its tool call through chunk folding instead of becoming an empty
# message.
# meiva:link agent/graph/llm/docs/llm.meiva.okf.md
from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk, UsageMetadata

from agent.graph.llm._chunk import assemble_final_message
from base.packages.plugin_providers import build_model_catalog

_STOP = {"model_provider": "google_genai", "finish_reason": "STOP"}
_USAGE = UsageMetadata(input_tokens=19065, output_tokens=560, total_tokens=19625)


def _fold(chunks: list[AIMessageChunk]) -> AIMessage:
    return assemble_final_message(chunks, catalog=build_model_catalog())


def test_thought_then_text_keeps_the_text() -> None:
    final = _fold(
        [
            AIMessageChunk(
                content=[{"type": "thinking", "thinking": "weigh the options", "index": 0}]
            ),
            AIMessageChunk(
                content=[{"type": "text", "text": "final answer", "index": 1}],
                response_metadata=dict(_STOP),
                usage_metadata=_USAGE,
            ),
        ]
    )

    assert final.text == "final answer"
    assert final.tool_calls == []


def test_text_and_tool_call_in_one_turn_keep_both() -> None:
    final = _fold(
        [
            AIMessageChunk(content=[{"type": "thinking", "thinking": "read the file", "index": 0}]),
            AIMessageChunk(content=[{"type": "text", "text": "Reading AGENTS.md.", "index": 1}]),
            AIMessageChunk(
                content=[],
                tool_call_chunks=[
                    {
                        "name": "execute_code",
                        "args": '{"code": "print(1)"}',
                        "id": "call_1",
                        "index": 0,
                    }
                ],
                response_metadata=dict(_STOP),
                usage_metadata=_USAGE,
            ),
        ]
    )

    assert final.text == "Reading AGENTS.md."
    assert [call["name"] for call in final.tool_calls] == ["execute_code"]
    assert final.tool_calls[0]["args"] == {"code": "print(1)"}
