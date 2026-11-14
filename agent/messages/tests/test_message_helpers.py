from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from agent.messages import (
    InboundTimes,
    NoteTag,
    exec_output_message,
    inbound_message,
    system_note_message,
)
from base.agents.messages.kwargs import ExecStatus
from base.agents.messages.timestamp_policy import TimestampPolicy

_PICKED_UP_AT = datetime(2026, 1, 1, tzinfo=UTC)


class TestInboundMessageMetadata:
    def test_returns_humanmessage(self):
        msg = inbound_message(
            content="hi",
            source="user",
            inbound_id=1,
            body_start=0,
            times=InboundTimes(arrived_at=None, picked_up_at=_PICKED_UP_AT),
        )
        assert isinstance(msg, HumanMessage)

    def test_content_passes_through(self):
        msg = inbound_message(
            content="hello world",
            source="user",
            inbound_id=2,
            body_start=0,
            times=InboundTimes(arrived_at=None, picked_up_at=_PICKED_UP_AT),
        )
        assert msg.content == "hello world"

    def test_additional_kwargs_exact_shape(self):
        msg = inbound_message(
            content="x",
            source="agent:42",
            inbound_id=3,
            body_start=0,
            times=InboundTimes(arrived_at=None, picked_up_at=_PICKED_UP_AT),
        )
        assert msg.additional_kwargs == {
            "meiva_msg_type": "inbound",
            "meiva_source": "agent:42",
            "meiva_inbound_id": 3,
            "meiva_inbound_body_start": 0,
            "meiva_picked_up_at": msg.additional_kwargs["meiva_picked_up_at"],
        }

    def test_source_value_propagates_distinct_inputs(self):
        for src in ("user", "agent:5", "system", "watcher:3"):
            msg = inbound_message(
                content="x",
                source=src,
                inbound_id=1,
                body_start=0,
                times=InboundTimes(arrived_at=None, picked_up_at=_PICKED_UP_AT),
            )
            assert msg.additional_kwargs["meiva_source"] == src


class TestMessageCreatedAtStamp:
    def test_inbound_message_stamps_when_given(self):
        from datetime import UTC, datetime

        dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        msg = inbound_message(
            content="hi",
            source="user",
            inbound_id=1,
            body_start=0,
            times=InboundTimes(arrived_at=dt, picked_up_at=_PICKED_UP_AT),
        )
        assert msg.additional_kwargs["meiva_created_at"] == "2026-06-19T15:30:00+00:00"

    def test_inbound_message_omits_when_absent(self):
        msg = inbound_message(
            content="hi",
            source="user",
            inbound_id=1,
            body_start=0,
            times=InboundTimes(arrived_at=None, picked_up_at=_PICKED_UP_AT),
        )
        assert "meiva_created_at" not in msg.additional_kwargs

    def test_exec_output_message_stamps_when_given(self):
        from datetime import UTC, datetime

        from agent.messages import exec_output_message

        dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        msg = exec_output_message(
            content="out",
            tool_call_id="t1",
            created_at=dt,
            status=ExecStatus.COMPLETED,
            body_start=0,
        )
        assert msg.additional_kwargs["meiva_created_at"] == "2026-06-19T15:30:00+00:00"

    def test_system_note_message_stamps_when_given(self):
        from datetime import UTC, datetime

        from agent.messages import NoteTag, system_note_message

        dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        msg = system_note_message(
            content="note",
            tag=NoteTag.MEMORY,
            created_at=dt,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        )
        assert msg.additional_kwargs["meiva_created_at"] == "2026-06-19T15:30:00+00:00"


def test_a_system_note_carries_both_tags() -> None:
    from agent.messages import NoteTag, system_note_message

    msg = system_note_message(
        content="line one\nline two",
        tag=NoteTag.MEMORY,
        picked_up_at=_PICKED_UP_AT,
        inputs=TimestampPolicy.off(),
    )

    assert msg.content == "<system_note>\nline one\nline two\n</system_note>"


def test_has_conversation_false_for_a_freshly_established_window() -> None:
    from langchain_core.messages import AnyMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, system_note_message

    head: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(
            content="your id",
            tag=NoteTag.AGENT_ID,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        ),
        system_note_message(
            content="the index",
            tag=NoteTag.MEMORY,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        ),
        system_note_message(
            content="your memory",
            tag=NoteTag.AGENT_MEMORY,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        ),
    ]
    assert has_conversation(head) is False


def test_has_conversation_false_for_an_empty_window() -> None:
    from agent.messages import has_conversation

    assert has_conversation([]) is False


def test_has_conversation_true_once_an_inbound_lands() -> None:
    from langchain_core.messages import AnyMessage, SystemMessage

    from agent.messages import (
        InboundTimes,
        NoteTag,
        has_conversation,
        inbound_message,
        system_note_message,
    )

    msgs: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(
            content="your id",
            tag=NoteTag.AGENT_ID,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        ),
        inbound_message(
            content="hello",
            source="user",
            inbound_id=1,
            body_start=0,
            times=InboundTimes(arrived_at=None, picked_up_at=_PICKED_UP_AT),
        ),
    ]
    assert has_conversation(msgs) is True


def test_has_conversation_true_for_an_agent_reply() -> None:
    from langchain_core.messages import AIMessage, AnyMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, system_note_message

    msgs: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(
            content="your id",
            tag=NoteTag.AGENT_ID,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        ),
        AIMessage(content="on it"),
    ]
    assert has_conversation(msgs) is True


def test_has_conversation_true_for_a_post_compact_summary() -> None:
    from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, system_note_message

    msgs: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(
            content="your id",
            tag=NoteTag.AGENT_ID,
            picked_up_at=_PICKED_UP_AT,
            inputs=TimestampPolicy.off(),
        ),
        HumanMessage(content="<system_note>\nYour context was just compacted. ...\n</system_note>"),
    ]
    assert has_conversation(msgs) is True


class TestPickedUpAtStamp:
    def test_inbound_keeps_arrival_and_adds_picked_up(self):
        from datetime import UTC, datetime

        arrival = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        picked_up = datetime(2026, 6, 19, 15, 31, tzinfo=UTC)
        msg = inbound_message(
            content="hi",
            source="user",
            inbound_id=1,
            body_start=0,
            times=InboundTimes(arrived_at=arrival, picked_up_at=picked_up),
        )
        kw = msg.additional_kwargs
        assert kw["meiva_created_at"] == arrival.isoformat()
        assert kw["meiva_picked_up_at"] == picked_up.isoformat()

    def test_each_builder_stamps_the_instant_it_is_given(self):
        from datetime import UTC, datetime

        from agent.messages import attach_message

        note_at = datetime(2026, 6, 19, 15, 31, tzinfo=UTC)
        attach_at = datetime(2026, 6, 19, 15, 32, tzinfo=UTC)
        inbound_at = datetime(2026, 6, 19, 15, 33, tzinfo=UTC)
        note = system_note_message(
            content="n",
            tag=NoteTag.MEMORY,
            created_at=attach_at,
            picked_up_at=note_at,
            inputs=TimestampPolicy.off(),
        )
        attach = attach_message(blocks=[], text="a", created_at=attach_at)
        inbound = inbound_message(
            content="x",
            source="user",
            inbound_id=1,
            body_start=0,
            times=InboundTimes(
                arrived_at=datetime(2020, 1, 1, tzinfo=UTC), picked_up_at=inbound_at
            ),
        )
        assert [m.additional_kwargs["meiva_picked_up_at"] for m in (note, attach, inbound)] == [
            note_at.isoformat(),
            attach_at.isoformat(),
            inbound_at.isoformat(),
        ]

    def test_exec_output_has_no_picked_up(self):
        from datetime import UTC, datetime

        msg = exec_output_message(
            content="o",
            tool_call_id="t",
            created_at=datetime.now(UTC),
            status=ExecStatus.COMPLETED,
            body_start=0,
        )
        assert "meiva_picked_up_at" not in msg.additional_kwargs


def test_system_note_writer_stamps_the_original_creation_instant_only_when_enabled() -> None:
    from datetime import UTC, datetime

    from base.agents.messages.timestamp_policy import TimestampPolicy

    created = datetime(2026, 11, 14, 8, 5, 51, tzinfo=UTC)
    picked = datetime(2026, 11, 14, 8, 9, 0, tzinfo=UTC)
    content = "A literal [2026-11-14 Sat 16:05:51] belongs to the body."
    enabled = TimestampPolicy(True, "Asia/Shanghai", True, picked)
    message = system_note_message(
        content=content,
        tag=NoteTag.CONTEXT,
        picked_up_at=picked,
        created_at=created,
        inputs=enabled,
    )
    assert message.content == f"<system_note>\n[2026-11-14 Sat 16:05:51] {content}\n</system_note>"
    disabled = system_note_message(
        content=content,
        tag=NoteTag.CONTEXT,
        picked_up_at=picked,
        created_at=created,
        inputs=TimestampPolicy.off(),
    )
    assert disabled.content == f"<system_note>\n{content}\n</system_note>"
    assert disabled.additional_kwargs == message.additional_kwargs
