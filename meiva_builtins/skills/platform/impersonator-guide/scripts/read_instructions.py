"""Print the native instructions inherited by an active impersonator."""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, SystemMessage

from base.agents.messages.kwargs import MeivaMsgType, NoteTag, read_meiva_kwargs
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
# Read native guidance under the attachment's authority checks. The attachment is opened and
# closed here, and closing revalidates the lease, so the text is selected and returned only
# after the lease held through detach. meiva.state also includes external journal replay, so
# the native snapshot is read separately: a controller's staged messages cannot replace the
# inherited instructions.
def read_instructions(target: Target) -> str:
    with impersonation.attach(target.session_id, agent_id=target.agent_id) as attached:
        messages: Sequence[BaseMessage] = attached.native_snapshot().messages
    return instruction_text(messages)


# meiva:note
# The session and agent named on the command line.
def parse_target(argv: Sequence[str]) -> Target:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", type=int)
    parser.add_argument("--agent", required=True, type=int, dest="agent_id")
    args = parser.parse_args(argv)
    return Target(session_id=args.session_id, agent_id=args.agent_id)


def main(argv: Sequence[str]) -> None:
    target = parse_target(argv)
    print(read_instructions(target))


if __name__ == "__main__":
    main(sys.argv[1:])
