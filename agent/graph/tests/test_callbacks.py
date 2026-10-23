"""RedisStreamHandler (agent/graph/_callbacks.py) unit tests.

Stream chunk shapes (after bind_tools with coerce_content_to_string=False):
- text_delta:    `content=[{"type":"text", "text":"...", "index":0}]`
- thinking_delta:`content=[{"type":"thinking", "thinking":"...", "index":0}]`
- signature_delta:`content=[{"type":"thinking", "signature":"...", "index":0}]` (skip)
- input_json_delta:`tool_call_chunks=[{"args":"...JSON frag...", "index":...}]`

Each event carries `item_id = f"{msg_idx}.{block_idx}"`, msg_idx comes from handler
ctor param (= len(state.messages) at llm_node entry). text/thinking block_idx
is content block position; each tool call's code block_idx = <text/thinking block count> +
<tool call appearance order> — provider-agnostic, not directly trusting chunk's raw index (gemini
gives None). Aligned with the id that the gateway timeline endpoint calculates for committed AIMessage,
so frontend merge uses stable keys to hit.

Note: tool-only synthetic chunks (no preceding content blocks) code lands on block 0; real
anthropic streams always have thinking/text before tool_use, so code lands on higher block.

Tests directly call `process_chunk(chunk)` + `finish()` + assert emitted args.
"""

from langchain_core.messages import AIMessageChunk

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.node_log.tests.live_events import LiveEvents
from base.agents.observation.turn_progress import TurnProgress
from base.events.live.projection import (
    ChatDelta,
    ChatStart,
    CodeDelta,
    CodeStart,
    LLMDone,
    ReasoningDelta,
    ReasoningStart,
)

# Test msg_idx — different tests use the same value, item_id is easy to compare
MSG_IDX = 5


def _text_chunk(text: str, index: int = 0) -> AIMessageChunk:
    return AIMessageChunk(content=[{"type": "text", "text": text, "index": index}])


def _thinking_chunk(thinking: str, index: int = 0) -> AIMessageChunk:
    return AIMessageChunk(content=[{"type": "thinking", "thinking": thinking, "index": index}])


def _signature_chunk(signature: str, index: int = 0) -> AIMessageChunk:
    """signature_delta block——server-signed opaque verifier, not user text."""
    return AIMessageChunk(content=[{"type": "thinking", "signature": signature, "index": index}])


def _tool_args_chunk(args_frag: str, index: int = 1) -> AIMessageChunk:
    """input_json_delta shape: tool_call_chunks=[{"args":..., "index":...}].
    Anthropic multiple arg-delta chunks for one tool call share the same index."""
    return AIMessageChunk(
        content=[],
        tool_call_chunks=[
            {"name": None, "args": args_frag, "id": None, "index": index},
        ],
    )


def _gemini_tool_chunk(args: str, call_id: str) -> AIMessageChunk:
    """gemini shape: one chunk gives full args, index=None, uses id to distinguish multiple tool calls."""
    return AIMessageChunk(
        content=[],
        tool_call_chunks=[
            {"name": "execute_code", "args": args, "id": call_id, "index": None},
        ],
    )


def _flush(handler: RedisStreamHandler) -> None:
    """Drain the handler's delta coalescer so buffered deltas are emitted
    (tests run without waiting out the 40ms SSE event window)."""
    handler.flush_deltas()


# meiva:note
# Feeds one chunk the way the stream loop does, with the monotonic time it arrived at. Tests that
# do not look at timings let every chunk arrive at the same instant.
def _process(handler: RedisStreamHandler, chunk: AIMessageChunk, now: float = 0.0) -> None:
    handler.process_chunk(chunk, now)


# Common item_id abbreviations. text/thinking at block_idx=0. tool-only stream (no preceding content
# blocks) code lands on block 0 = CODE_ID; with preceding content blocks code lands on higher block.
TEXT_ID = f"{MSG_IDX}.0"
CODE_ID = f"{MSG_IDX}.0"


async def test_streams_code_coalesced_from_partial_json(live_events: LiveEvents):
    """args incremental fragments accumulate into valid JSON, each intermediate publish of current code delta.
    No preceding content blocks → code lands on block 0."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _tool_args_chunk('{"co'))
    _process(handler, _tool_args_chunk('de":"pri'))
    _process(handler, _tool_args_chunk('nt(2)"}'))

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        CodeStart(agent_id=42, item_id=CODE_ID),
        CodeDelta(agent_id=42, item_id=CODE_ID, content="print(2)"),
    ]


async def test_decodes_json_escapes(live_events: LiveEvents):
    """JSON escapes (\\n / \\\") auto-decoded before publish — frontend gets native Python."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _tool_args_chunk('{"code":"print(\\"hi\\")\\n"}'))
    _flush(handler)
    events = await live_events.typed()
    deltas = [e for e in events if isinstance(e, CodeDelta)]
    assert "".join(d.content for d in deltas) == 'print("hi")\n'


async def test_text_block_publishes_chat_streaming(live_events: LiveEvents):
    """text content block → ChatStart (first time for block) + ChatDelta (each block delta)."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _text_chunk("hello "))
    _process(handler, _text_chunk("user"))

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        ChatStart(agent_id=42, item_id=TEXT_ID),
        ChatDelta(agent_id=42, item_id=TEXT_ID, content="hello user"),
    ]


async def test_string_content_publishes_chat_streaming(live_events: LiveEvents):
    """legacy / coerce_content_to_string=True path: chunk.content is a string.
    Unit test constructs `AIMessageChunk(content="hello")` for simplicity — handler must still
    publish ChatDelta (block_idx=0)."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, AIMessageChunk(content="hello "))
    _process(handler, AIMessageChunk(content="user"))

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        ChatStart(agent_id=42, item_id=TEXT_ID),
        ChatDelta(agent_id=42, item_id=TEXT_ID, content="hello user"),
    ]


async def test_empty_text_block_no_chat_publish(live_events: LiveEvents):
    """Empty text content (tool_call-only chunk) does not emit ChatStart — avoids empty agent_chat block."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _tool_args_chunk('{"code":"x"}'))

    _flush(handler)
    events = await live_events.typed()
    assert all(not isinstance(e, ChatStart | ChatDelta) for e in events)


async def test_streams_reasoning_progressively(live_events: LiveEvents):
    """thinking content blocks are chunk-level text incrementals, directly published."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _thinking_chunk("Let me "))
    _process(handler, _thinking_chunk("think... "))
    _process(handler, _thinking_chunk("OK"))

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        ReasoningStart(agent_id=42, item_id=TEXT_ID),
        ReasoningDelta(agent_id=42, item_id=TEXT_ID, content="Let me think... OK"),
    ]


# meiva:note
# reasoning_ms_by_block[idx] = (last - first thinking token of that block) * 1000, measured off
# the arrival times the caller passes. Empty before any thinking streams.
async def test_reasoning_ms_measures_first_to_last_thinking_token(live_events: LiveEvents):
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())
    assert handler.reasoning_ms_by_block == {}

    _process(handler, _thinking_chunk("a"), 100.0)
    _process(handler, _thinking_chunk("b"), 100.5)
    _process(handler, _thinking_chunk("c"), 108.2)
    assert handler.reasoning_ms_by_block == {0: 8200}


async def test_reasoning_ms_per_block_independent(live_events: LiveEvents):
    """Interleaved thinking across two blocks: each block is timed on its own
    first/last token, so the gap between them (where text would stream) is
    never folded into a thinking duration."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())
    # block0: 100.0 -> 102.0 (2000ms); block2: 105.0 -> 106.5 (1500ms).
    _process(handler, _thinking_chunk("a", index=0), 100.0)
    _process(handler, _thinking_chunk("b", index=0), 102.0)
    _process(handler, _thinking_chunk("c", index=2), 105.0)
    _process(handler, _thinking_chunk("d", index=2), 106.5)
    assert handler.reasoning_ms_by_block == {0: 2000, 2: 1500}


async def test_reasoning_ms_empty_without_thinking(live_events: LiveEvents):
    """A code-only turn (no thinking) leaves reasoning_ms_by_block empty."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())
    _process(handler, _tool_args_chunk('{"code":"x"}'))
    assert handler.reasoning_ms_by_block == {}


async def test_signature_delta_skipped(live_events: LiveEvents):
    """signature_delta block (type=thinking but only carries signature field) skipped —
    server-signed opaque verifier, not user-visible text."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _thinking_chunk("Let me think"))
    _process(handler, _signature_chunk("opaque-sig-bytes"))

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        ReasoningStart(agent_id=42, item_id=TEXT_ID),
        ReasoningDelta(agent_id=42, item_id=TEXT_ID, content="Let me think"),
    ]


async def test_reasoning_then_code_independent_streams(live_events: LiveEvents):
    """reasoning phase comes first, then code phase — two independent streams, code offset past thinking blocks.
    thinking@0 → code lands on block 1."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _thinking_chunk("planning..."))
    _process(handler, _tool_args_chunk('{"code":"x"}'))

    code_id = f"{MSG_IDX}.1"
    _flush(handler)
    events = await live_events.typed()
    assert ReasoningStart(agent_id=42, item_id=TEXT_ID) in events
    assert ReasoningDelta(agent_id=42, item_id=TEXT_ID, content="planning...") in events
    assert CodeStart(agent_id=42, item_id=code_id) in events
    assert CodeDelta(agent_id=42, item_id=code_id, content="x") in events


async def test_finish_flushes_remaining_code_and_publishes_llm_done(live_events: LiveEvents):
    """At stream end, partial JSON finally becomes valid → finish() publish remainder as fallback, then LLMDone."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _tool_args_chunk('{"code":""'))
    assert await live_events.payloads() == []

    # Simulate the last args fragment making code become "x" — directly modify buf. No preceding content
    # block, so the first tool call's code block_idx=0, buf dict key aligns with 0.
    handler._args_bufs[0] = '{"code":"x"}'

    handler.finish()

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        CodeStart(agent_id=42, item_id=CODE_ID),
        CodeDelta(agent_id=42, item_id=CODE_ID, content="x"),
        LLMDone(agent_id=42),
    ]


async def test_finish_publishes_llm_done_even_when_no_code(live_events: LiveEvents):
    """No tool_call (text-only / reasoning-only / no output) should still publish LLMDone."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _text_chunk("just chatting"))
    handler.finish()

    _flush(handler)
    events = await live_events.typed()
    assert events == [
        ChatStart(agent_id=42, item_id=TEXT_ID),
        ChatDelta(agent_id=42, item_id=TEXT_ID, content="just chatting"),
        LLMDone(agent_id=42),
    ]


async def test_multi_block_chunk_dispatches_both(live_events: LiveEvents):
    """Same chunk carries both text + thinking multiple blocks (rare but legal) → each block_idx
    different *Start, item_id distinct."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=42, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    chunk = AIMessageChunk(
        content=[
            {"type": "thinking", "thinking": "first thought", "index": 0},
            {"type": "text", "text": "hello", "index": 1},
        ]
    )
    _process(handler, chunk)
    _flush(handler)
    events = await live_events.typed()
    # Starts are immediate (frontend needs the item), deltas coalesce and
    # flush after the window — reasoning key first-appended, so it flushes first.
    assert events == [
        ReasoningStart(agent_id=42, item_id=f"{MSG_IDX}.0"),
        ChatStart(agent_id=42, item_id=f"{MSG_IDX}.1"),
        ReasoningDelta(agent_id=42, item_id=f"{MSG_IDX}.0", content="first thought"),
        ChatDelta(agent_id=42, item_id=f"{MSG_IDX}.1", content="hello"),
    ]


async def test_handler_agent_id_carried_per_instance(live_events: LiveEvents):
    """Two handler instances don't interfere — agent_id + msg_idx bound to instance."""
    pub = live_events.publisher
    h1 = RedisStreamHandler(pub, agent_id=1, msg_idx=3, turn_progress=TurnProgress())
    h2 = RedisStreamHandler(pub, agent_id=2, msg_idx=3, turn_progress=TurnProgress())

    _process(h1, _tool_args_chunk('{"code":"a"}'))
    _process(h2, _tool_args_chunk('{"code":"b"}'))
    _flush(h1)
    _flush(h2)
    events = await live_events.typed()
    starts = [e for e in events if isinstance(e, CodeStart)]
    deltas = [e for e in events if isinstance(e, CodeDelta)]
    assert {s.agent_id for s in starts} == {1, 2}
    # Two independent handlers, each tool-only stream (no preceding content blocks) → each code block 0
    assert {(d.agent_id, d.item_id, d.content) for d in deltas} == {
        (1, "3.0", "a"),
        (2, "3.0", "b"),
    }


async def test_anthropic_thinking_text_then_tool_offsets_code(live_events: LiveEvents):
    """anthropic shape: thinking@0 + text@1 + tool_use (index=2) → code lands on block 2,
    matching content_block_index (tool_use always after narration/reasoning)."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=7, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _thinking_chunk("plan", index=0))
    _process(handler, _text_chunk("hi", index=1))
    _process(handler, _tool_args_chunk('{"code":"go()"}', index=2))
    _flush(handler)

    code_id = f"{MSG_IDX}.2"
    _flush(handler)
    events = await live_events.typed()
    assert CodeStart(agent_id=7, item_id=code_id) in events
    assert CodeDelta(agent_id=7, item_id=code_id, content="go()") in events


async def test_gemini_text_then_tool_index_none_offsets_code(live_events: LiveEvents):
    """gemini shape: text@0 + one tool_call_chunk (index=None, full args) → code lands on
    block 1, not colliding with text's block 0 id."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=8, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _text_chunk("Let me compute that.", index=0))
    _process(handler, _gemini_tool_chunk('{"code": "print(2 + 3)"}', "call-0"))
    _flush(handler)
    events = await live_events.typed()
    assert ChatStart(agent_id=8, item_id=TEXT_ID) in events
    code_id = f"{MSG_IDX}.1"
    assert CodeStart(agent_id=8, item_id=code_id) in events
    assert CodeDelta(agent_id=8, item_id=code_id, content="print(2 + 3)") in events


async def test_gemini_thinking_then_text_then_tool_publishes_reasoning(live_events: LiveEvents):
    """gemini (include_thoughts=True) shape: thought returned as `{"type":"thinking",
    "thinking":...}` content block — exactly the same shape as anthropic, so handler needs no
    gemini-specific branch to emit ReasoningStart/Delta. Regression: locks "gemini reasoning goes through
    thinking-block path", if handler degenerates to anthropic-only this test will red.
    Shape taken from real gemini-3.5-flash astream trace."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=8, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _thinking_chunk("**Analyzing the Snail's Ascent**\n", index=0))
    _process(handler, _thinking_chunk("It reaches the top on day 8.", index=0))
    _process(handler, _text_chunk("Day 8.", index=1))
    _process(handler, _gemini_tool_chunk('{"code": "print(8)"}', "call-0"))
    _flush(handler)
    events = await live_events.typed()
    reasoning = [e for e in events if isinstance(e, ReasoningStart | ReasoningDelta)]
    assert reasoning == [
        ReasoningStart(agent_id=8, item_id=TEXT_ID),
        ReasoningDelta(
            agent_id=8,
            item_id=TEXT_ID,
            content="**Analyzing the Snail's Ascent**\nIt reaches the top on day 8.",
        ),
    ]
    # text → chat block 1, tool → code block 2 (past thinking@0 + text@1)
    assert ChatStart(agent_id=8, item_id=f"{MSG_IDX}.1") in events
    assert CodeDelta(agent_id=8, item_id=f"{MSG_IDX}.2", content="print(8)") in events


async def test_gemini_multiple_tools_index_none_distinct_blocks(live_events: LiveEvents):
    """gemini shape: multiple tool calls at once, all index=None, distinguished by id → each independent code
    block (0/1/2), args not concatenated into illegal JSON (regression: old impl collapsed all into block 0 making bad JSON)."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=9, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _gemini_tool_chunk('{"code": "print(1)"}', "c0"))
    _process(handler, _gemini_tool_chunk('{"code": "print(2)"}', "c1"))
    _process(handler, _gemini_tool_chunk('{"code": "print(3)"}', "c2"))
    _flush(handler)
    events = await live_events.typed()
    deltas = [e for e in events if isinstance(e, CodeDelta)]
    assert {(d.item_id, d.content) for d in deltas} == {
        (f"{MSG_IDX}.0", "print(1)"),
        (f"{MSG_IDX}.1", "print(2)"),
        (f"{MSG_IDX}.2", "print(3)"),
    }


async def test_openai_string_content_then_incremental_tool_args(live_events: LiveEvents):
    """openai (gpt-*) shape: content is string chunk (not list-of-blocks), tool args
    incremental across chunks (index=0, first chunk carries id, subsequent empty). chat lands block 0, code offset to
    block 1, and code progressively streamed (unlike gemini that gives full args at once)."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=11, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, AIMessageChunk(content="Hi"))
    _process(handler, AIMessageChunk(content="!"))
    # tool args incremental: first chunk carries id, subsequent id empty, all index=0
    frags = [("call_x", '{"'), ("", "code"), ("", '":"'), ("", "go()"), ("", '"}')]
    for call_id, frag in frags:
        _process(
            handler,
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "execute_code", "args": frag, "id": call_id, "index": 0}
                ],
            ),
        )
    _flush(handler)
    events = await live_events.typed()
    code_id = f"{MSG_IDX}.1"
    assert ChatStart(agent_id=11, item_id=TEXT_ID) in events
    code_deltas = [e for e in events if isinstance(e, CodeDelta)]
    # Progressive streaming: multiple deltas accumulate to full code, all in block 1
    assert all(d.item_id == code_id for d in code_deltas)
    assert "".join(d.content for d in code_deltas) == "go()"


def _openai_reasoning_chunk(frag: str, index: int = 0) -> AIMessageChunk:
    """openai Responses API reasoning summary chunk: visible text nested in
    summary[].text (not a flat field). Captured from real gpt-5.4-mini astream."""
    return AIMessageChunk(
        content=[
            {
                "type": "reasoning",
                "summary": [{"index": 0, "type": "summary_text", "text": frag}],
                "index": index,
            }
        ]
    )


async def test_openai_responses_reasoning_summary_publishes_reasoning(live_events: LiveEvents):
    """openai Responses reasoning block (text in summary[].text) folded to the
    canonical thinking shape by base.lm.reasoning before _process_content, so the
    handler publishes ReasoningStart/Delta with no openai-specific branch. Real
    turn order: reasoning@0 → text@1 → tool@2, code offset past both content
    blocks (so the streamed item_id matches the committed snapshot)."""
    pub = live_events.publisher
    handler = RedisStreamHandler(pub, agent_id=11, msg_idx=MSG_IDX, turn_progress=TurnProgress())

    _process(handler, _openai_reasoning_chunk("**Plan**\n", index=0))
    _process(handler, _openai_reasoning_chunk("Compute it.", index=0))
    _process(handler, _text_chunk("The answer is 8.", index=1))
    _process(handler, _tool_args_chunk('{"code":"print(8)"}', index=2))
    _flush(handler)
    events = await live_events.typed()
    reasoning = [e for e in events if isinstance(e, ReasoningStart | ReasoningDelta)]
    assert reasoning == [
        ReasoningStart(agent_id=11, item_id=TEXT_ID),
        ReasoningDelta(agent_id=11, item_id=TEXT_ID, content="**Plan**\nCompute it."),
    ]
    assert ChatStart(agent_id=11, item_id=f"{MSG_IDX}.1") in events
    assert CodeDelta(agent_id=11, item_id=f"{MSG_IDX}.2", content="print(8)") in events


async def test_reset_restores_fresh_stream_state_for_retry(live_events: LiveEvents):
    """The stale-cache retry path re-streams the same message through the same
    handler. Without reset(), started sets suppress Start events while deltas
    re-append (doubled partial text) and args bufs concatenate (code delta
    stalls). After reset(), a second stream must be byte-identical to a
    fresh handler's."""
    pub = live_events.publisher
    h1 = RedisStreamHandler(pub, 42, MSG_IDX, turn_progress=TurnProgress())
    _process(h1, _text_chunk("Hello"))
    _process(h1, _tool_args_chunk('{"code": "print(1)"}'))
    _flush(h1)  # drain the coalescer so the compare covers deltas, not just starts
    first_events = await live_events.payloads()

    h1.reset()
    _process(h1, _text_chunk("Hello"))
    _process(h1, _tool_args_chunk('{"code": "print(1)"}'))
    _flush(h1)
    second_events = await live_events.payloads()
    assert second_events == first_events  # start events re-emitted, same deltas

    # timing accumulators must not double-count across attempts
    assert h1.reasoning_ms_by_block == {}
    assert h1.code_ms_by_block != {}


async def test_without_reset_streaming_duplicates_content(live_events: LiveEvents):
    """The bug reset() fixes: re-streaming through a used handler re-appends
    deltas to already-started blocks (frontend sees 'HelloHello')."""
    pub = live_events.publisher
    h = RedisStreamHandler(pub, 42, MSG_IDX, turn_progress=TurnProgress())
    _process(h, _text_chunk("Hello"))
    await live_events.payloads()
    _process(h, _text_chunk("Hello"))  # no reset — the old retry behavior
    _flush(h)
    events = await live_events.payloads()
    # no ChatStart (already started); both "Hello"s coalesce into ONE delta —
    # the duplicated content is still visible (frontend sees "HelloHello")
    assert all("chat_start" not in e for e in events)
    deltas = [e for e in events if "chat_delta" in e]
    assert len(deltas) == 1
    assert '"HelloHello"' in deltas[0]
