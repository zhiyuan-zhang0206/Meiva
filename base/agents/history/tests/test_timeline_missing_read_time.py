from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.messages.envelope import inbound_head, wrap_inbound
from base.agents.messages.kwargs import MeivaMsgType, NoteTag
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import Clock
from base.config import settings

_INPUTS = TimestampPolicy.from_clock(
    Clock.from_settings(), timestamps_enabled=settings.general.message_timestamps
)


def _render(*messages: BaseMessage) -> list[TimelineItem]:
    items, count = build_timeline_items(list(messages), inputs=_INPUTS)
    assert count == len(messages)
    return items


def test_every_message_shape_without_a_read_time_renders_with_none() -> None:
    items = _render(
        HumanMessage(
            content="hello",
            additional_kwargs={
                "meiva_msg_type": MeivaMsgType.INBOUND.value,
                "meiva_source": "user",
            },
        ),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "hmm", "index": 0},
                {"type": "text", "text": "done", "index": 1},
            ],
            tool_calls=[{"name": "execute_code", "args": {"code": "1"}, "id": "c1"}],
        ),
        ToolMessage(
            content="out",
            tool_call_id="c1",
            additional_kwargs={"meiva_msg_type": MeivaMsgType.EXEC_OUTPUT.value},
        ),
        HumanMessage(
            content="note",
            additional_kwargs={
                "meiva_msg_type": MeivaMsgType.SYSTEM_NOTE.value,
                "meiva_note_tag": NoteTag.MEMORY.value,
            },
        ),
        HumanMessage(content="attached", additional_kwargs={"meiva_msg_type": "attach"}),
        HumanMessage(content="plain"),
    )
    assert [item.kind for item in items] == [
        "inbound_chat",
        "agent_reasoning",
        "agent_chat",
        "agent_code",
        "code_output",
        "system_marker",
        "attach",
        "system_marker",
    ]
    assert [item.created_at for item in items] == [None] * len(items)


def test_an_inbound_without_an_embedded_id_renders_no_inbound_id() -> None:
    (item,) = _render(
        HumanMessage(
            content="hello",
            additional_kwargs={
                "meiva_msg_type": MeivaMsgType.INBOUND.value,
                "meiva_created_at": "2026-10-13T08:00:00+00:00",
            },
        )
    )
    assert item.inbound_id is None
    assert item.created_at == "2026-10-13T08:00:00+00:00"


def test_the_read_time_is_the_pickup_time_else_the_creation_time() -> None:
    picked, created = _render(
        HumanMessage(
            content="picked up",
            additional_kwargs={
                "meiva_msg_type": MeivaMsgType.INBOUND.value,
                "meiva_created_at": "2026-10-13T08:00:00+00:00",
                "meiva_picked_up_at": "2026-10-13T08:00:05+00:00",
            },
        ),
        AIMessage(
            content="answer", additional_kwargs={"meiva_created_at": "2026-10-13T08:00:09+00:00"}
        ),
    )
    assert picked.created_at == "2026-10-13T08:00:05+00:00"
    assert created.created_at == "2026-10-13T08:00:09+00:00"


def test_a_mixed_window_keeps_each_message_its_own_time() -> None:
    stamped, missing = _render(
        AIMessage(
            content="old", additional_kwargs={"meiva_created_at": "2026-10-13T08:00:00+00:00"}
        ),
        AIMessage(content="older still, never stamped"),
    )
    assert stamped.created_at == "2026-10-13T08:00:00+00:00"
    assert missing.created_at is None


@pytest.mark.parametrize(
    "source", ["user", "agent:7", "schedule:1", "watcher:1", "system", "system:upload"]
)
@pytest.mark.parametrize("timestamps", [False, True])
@pytest.mark.parametrize("multimodal", [False, True])
def test_inbound_payload_is_writer_body_in_every_projection(
    source: str, timestamps: bool, multimodal: bool
) -> None:
    policy = TimestampPolicy(
        timestamps_enabled=timestamps,
        timezone="UTC",
        weekday=True,
        now=datetime(2026, 11, 14, tzinfo=UTC),
    )
    body = (
        "[2026-11-14 Sat 16:05:51]\n\nUser:\n\nAgent 7:\n\n<system_note>\nliteral\n</system_note>"
    )
    head = inbound_head(source, inputs=policy)
    wrapped = wrap_inbound(body, source, inputs=policy)
    content: str | list[str | dict[str, object]] = (
        [
            {"type": "text", "text": wrapped},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
            {"type": "text", "text": "[image unavailable]"},
        ]
        if multimodal
        else wrapped
    )
    message = HumanMessage(
        content=content,
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_source": source,
            "meiva_inbound_body_start": len(head),
            "meiva_inbound_id": 1,
        },
    )
    expected = body + ("\n[image unavailable]" if multimodal else "")
    for prefix in ("", "s1"):
        items, _ = build_timeline_items([message], inputs=policy, segment_prefix=prefix)
        assert items[0].payload == expected
        assert items[0].source == source
        assert items[0].inbound_id == 1


@pytest.mark.parametrize("start", [None, -1, 10000, True, "5"])
@pytest.mark.parametrize("source", ["user", "system"])
def test_legacy_or_invalid_inbound_offset_preserves_all_text(start: object, source: str) -> None:
    content = "<system_note>\nFirst paragraph\n\nSecond paragraph\n</system_note>"
    kwargs: dict[str, object] = {"meiva_msg_type": "inbound", "meiva_source": source}
    if start is not None:
        kwargs["meiva_inbound_body_start"] = start
    message = HumanMessage(content=content, additional_kwargs=kwargs)
    assert build_timeline_items([message], inputs=TimestampPolicy.off())[0][0].payload == content


@pytest.mark.parametrize("header", ["User:\n\n", "[2026-11-14 Sat 16:05:51]\n\n"])
def test_attachment_sentinel_is_normalized_by_recorded_offset(header: str) -> None:
    message = HumanMessage(
        content=header + "[files]",
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_source": "user",
            "meiva_inbound_body_start": len(header),
        },
    )
    assert build_timeline_items([message], inputs=TimestampPolicy.off())[0][0].payload == "[files]"


def test_literal_system_note_in_human_body_is_not_a_model_envelope() -> None:
    body = "<system_note>\nliteral\n</system_note>"
    message = HumanMessage(
        content=body,
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_source": "user",
            "meiva_inbound_body_start": 0,
        },
    )
    assert build_timeline_items([message], inputs=TimestampPolicy.off())[0][0].payload == body
