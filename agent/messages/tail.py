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
# The `meiva_source` every inbound message carries. The bag comes off a stored LangChain
# message, so the value is narrowed here rather than trusted from the TypedDict: a missing
# source still raises the KeyError callers relied on, and a non-string one stops the caller
# instead of dropping the message silently — the failure names the message class, its inbound
# id and a clipped repr of what the field held, because the alternative (a peer inbound
# disappearing with no record) is the failure this read exists to prevent.
# (`base.host.boundary.fields.str_field` is the same read, but this module is `strict` and that
# one is not, so importing it would break the manifest.)
def _source_of(msg: AnyMessage) -> str:
    kwargs = read_meiva_kwargs(msg)
    source = kwargs.get("meiva_source")
    if not isinstance(source, str):
        where = (
            f"meiva_msg_type={kwargs.get('meiva_msg_type')!r}, "
            f"meiva_inbound_id={kwargs.get('meiva_inbound_id')!r}, "
            f"meiva_source={source!r:.120}"
        )
        if "meiva_source" not in kwargs:
            raise KeyError(f"meiva_source is missing ({where})")
        raise TypeError(f"meiva_source is {type(source).__name__}, not str ({where})")
    return source


# meiva:note
# The `meiva_source` of every inbound added since the last assistant turn.
# Scans backward from the end, stopping at the most recent AIMessage.
def _tail_inbound_sources(messages: list[AnyMessage]) -> list[str]:
    sources: list[str] = []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            break
        if not isinstance(msg, HumanMessage):
            continue
        if read_meiva_kwargs(msg).get("meiva_msg_type") != MeivaMsgType.INBOUND:
            continue
        sources.append(_source_of(msg))
    return sources


# meiva:note
# Whether the messages added since the last assistant turn include an inbound
# from another agent (`meiva_source` starts with "agent:"). Used by before_llm
# hooks that coordinate the single `messages` write a pass allows.
def tail_has_agent_inbound(messages: list[AnyMessage]) -> bool:
    return any(s.startswith("agent:") for s in _tail_inbound_sources(messages))


# meiva:note
# The newest `agent:`-sourced inbound after the last assistant turn: the one message a peer
# gate reads, never the conversation. Every inbound-typed message carries the contract field, so
# a source that is missing or not a string stops the scan (`_source_of`) instead of skipping the
# message it belongs to.
# meiva:link agent/messages/docs/message-format.meiva.okf.md
def last_agent_inbound_text(messages: list[AnyMessage]) -> str | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return None
        if not isinstance(msg, HumanMessage):
            continue
        if read_meiva_kwargs(msg).get("meiva_msg_type") != MeivaMsgType.INBOUND:
            continue
        if not _source_of(msg).startswith("agent:"):
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
