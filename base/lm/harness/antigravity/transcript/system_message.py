from __future__ import annotations

import re
from dataclasses import dataclass

from base.lm.harness.antigravity.transcript.errors import UnrecognisedError
from base.lm.harness.events import HarnessCause

# meiva:note
# What a `SYSTEM_MESSAGE` row says, from its `content` alone (the record keeps no `eventType` and no
# `hideFromUser` flag; those are trajectory fields). The row is either the CLI's own restart notice,
# which is noise, or an agent message in the envelope
# `[Message] timestamp=<utc> sender=<id> priority=<p> content=<text>`:
#   sender `<conversation>/task-<n>` is a task the conversation started. Its content is either
#   `Task id "<conversation>/task-<n>" finished with result: ...` (a background command ended) or the
#   prompt a timer was set with (the timer fired);
#   sender `system` with other content is shown with cause UNKNOWN;
#   any other sender is another conversation, a sub-agent's report or a parent's task.
# A row that is not the restart notice and has no envelope is `UnrecognisedError`, so a new kind of notice is
# reported instead of shown wrongly.
# meiva:link base/lm/harness/docs/subagent-text.md

_RESTART_MARKER = "have been stopped due to server restart"
_ENVELOPE = re.compile(
    r"\A\[Message\] timestamp=(\S+) sender=(\S+) priority=(\S+) content=(.*)\Z", re.DOTALL
)
_TASK_SENDER = re.compile(r"/task-\d+\Z")
_TASK_FINISHED = 'Task id "'


@dataclass(frozen=True, slots=True)
class SystemNotice:
    text: str
    cause: HarnessCause
    sender: str


def classify(content: str) -> SystemNotice | None:
    if "[Notice]" in content and _RESTART_MARKER in content:
        return None
    wrapped = re.search(
        r"<SYSTEM_MESSAGE>\s*(\[Message\].*?)\s*</SYSTEM_MESSAGE>", content, re.DOTALL
    )
    if wrapped is not None:
        content = wrapped.group(1)
    found = _ENVELOPE.match(content)
    if found is None:
        raise UnrecognisedError(f"system_message:{content.split(maxsplit=1)[0]}")
    sender, text = found.group(2), found.group(4).strip()
    if sender == "system":
        return None
    if not text:
        raise UnrecognisedError("system_message:empty")
    return SystemNotice(text=text, cause=_cause(sender, text), sender=sender)


def _cause(sender: str, text: str) -> HarnessCause:
    if _TASK_SENDER.search(sender):
        return (
            HarnessCause.BACKGROUND_TASK
            if text.startswith(_TASK_FINISHED)
            else HarnessCause.SCHEDULED_WAKE
        )
    if sender == "system":
        return HarnessCause.UNKNOWN
    return HarnessCause.BACKGROUND_TASK
