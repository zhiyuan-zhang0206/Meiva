"""Print the native instructions inherited by an active impersonator."""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, SystemMessage

from base.agents.messages.identity import is_message_likes
from base.agents.messages.kwargs import MeivaMsgType, NoteTag, read_meiva_kwargs
from base.host.boundary.options import options
from base.host.boundary.untyped import object_of
from meiva import impersonation


def instruction_text(messages: Sequence[BaseMessage]) -> str:
    """Select the native system prompt and configured preloaded skill notes."""
    if not messages or not isinstance(messages[0], SystemMessage):
        raise RuntimeError("borrowed agent has no saved system prompt")
    parts: list[str] = []
    for index, message in enumerate(messages):
        metadata = read_meiva_kwargs(message)
        if index != 0 and not (
            metadata.get("meiva_msg_type") == MeivaMsgType.SYSTEM_NOTE
            and metadata.get("meiva_note_tag") == NoteTag.PRELOADED_SKILLS
        ):
            continue
        if not isinstance(message.content, str) or not message.content.strip():
            raise ValueError("borrowed agent instructions must contain nonempty text")
        parts.append(message.content)
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Target:
    session_id: int
    agent_id: int


# meiva:note
# `native_snapshot()` answers `object` — the borrowed agent's saved state, named no closer at the SDK
# edge so that `import meiva` stays free of the state schema — so its message list is read with
# `object_of` and checked here instead of annotated: a snapshot with no message sequence, or one
# holding something that is not a message, is refused by name before `instruction_text` decides
# which of the messages are the inherited instructions.
def snapshot_messages(snapshot: object) -> Sequence[BaseMessage]:
    messages = object_of(getattr, snapshot, "messages", None)
    if not is_message_likes(messages):
        raise TypeError("the borrowed agent's snapshot carries no message sequence")
    kept: list[BaseMessage] = []
    for message in messages:
        if not isinstance(message, BaseMessage):
            raise TypeError(
                f"the borrowed agent's snapshot carries {type(message).__name__}, not a message"
            )
        kept.append(message)
    return kept


# meiva:note
# Read native guidance under the attachment's authority checks. The attachment is opened and
# closed here, and closing revalidates the lease, so the text is selected and returned only
# after the lease held through detach. meiva.state also includes external journal replay, so
# the native snapshot is read separately: a controller's staged messages cannot replace the
# inherited instructions.
def read_instructions(target: Target) -> str:
    with impersonation.attach(target.session_id, agent_id=target.agent_id) as attached:
        messages = snapshot_messages(attached.native_snapshot())
    return instruction_text(messages)


# meiva:note
# The session and agent named on the command line, read once through `options` as the `Target` the
# rest of the file takes: the namespace's attributes are `Any`, so this is the one place that reads
# them.
def parse_target(argv: Sequence[str]) -> Target:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", type=int)
    parser.add_argument("--agent", required=True, type=int, dest="agent_id")
    return options(parser.parse_args(argv), Target)


def main(argv: Sequence[str]) -> None:
    target = parse_target(argv)
    print(read_instructions(target))


if __name__ == "__main__":
    main(sys.argv[1:])
