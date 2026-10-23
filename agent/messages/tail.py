from __future__ import annotations

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from base.agents.messages.kwargs import MeivaMsgType, read_meiva_kwargs

__all__ = [
    "has_conversation",
    "last_agent_inbound_text",
    "tail_has_agent_inbound",
    "tail_has_recallable_inbound",
    "tail_has_skill_recall_inbound",
]


# meiva:note
# The `meiva_source` of every inbound added since the last assistant turn.
# Scans backward from the end, stopping at the most recent AIMessage, and
# indexes `meiva_source` off the raw bag so a missing source on an inbound
# message surfaces as a KeyError instead of being silently dropped.
def _tail_inbound_sources(messages: list[AnyMessage]) -> list[str]:
    sources: list[str] = []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            break
        if not isinstance(msg, HumanMessage):
            continue
        if read_meiva_kwargs(msg).get("meiva_msg_type") != MeivaMsgType.INBOUND:
            continue
        source = msg.additional_kwargs["meiva_source"]  # pyright: ignore[reportUnknownMemberType]
        if isinstance(source, str):
            sources.append(source)
    return sources


# meiva:note
# Whether the messages added since the last assistant turn include an inbound
# from another agent (`meiva_source` starts with "agent:"). Used by before_llm
# hooks that coordinate the single `messages` write a pass allows.
def tail_has_agent_inbound(messages: list[AnyMessage]) -> bool:
    return any(s.startswith("agent:") for s in _tail_inbound_sources(messages))


# meiva:note
# The newest `agent:`-sourced inbound after the last assistant turn: the one message a peer
# gate reads, never the conversation. Reads `meiva_source` by index, not `.get`, because every
# inbound-typed message carries the contract field and a missing source must surface as a
# KeyError instead of being silently skipped.
# meiva:link agent/messages/docs/message-format.meiva.okf.md
def last_agent_inbound_text(messages: list[AnyMessage]) -> str | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return None
        if not isinstance(msg, HumanMessage):
            continue
        if read_meiva_kwargs(msg).get("meiva_msg_type") != MeivaMsgType.INBOUND:
            continue
        source = msg.additional_kwargs["meiva_source"]  # pyright: ignore[reportUnknownMemberType]
        if not (isinstance(source, str) and source.startswith("agent:")):
            continue
        content = msg.content
        return content if isinstance(content, str) else None
    return None


# meiva:note
# Whether anything beyond the standing head (SystemMessage and context notes)
# has been said in this context window yet.
def has_conversation(messages: list[AnyMessage]) -> bool:
    return any(
        not isinstance(m, SystemMessage)
        and read_meiva_kwargs(m).get("meiva_msg_type") != MeivaMsgType.SYSTEM_NOTE
        for m in messages
    )


# meiva:note
# Whether the messages added since the last assistant turn include an inbound
# passive memory recall should fire on: any source except a background wake-up
# (`watcher:` / `shell:` prefixes).
def tail_has_recallable_inbound(messages: list[AnyMessage]) -> bool:
    return any(not s.startswith(("watcher:", "shell:")) for s in _tail_inbound_sources(messages))


# meiva:note
# Whether the messages added since the last assistant turn include an inbound
# that skill recall should fire on: a human message (`user`, `ui:page:<name>`)
# or a schedule trigger (`schedule:<id>`).
def tail_has_skill_recall_inbound(messages: list[AnyMessage]) -> bool:
    return any(
        s == "user" or s.startswith(("ui:page:", "schedule:"))
        for s in _tail_inbound_sources(messages)
    )
