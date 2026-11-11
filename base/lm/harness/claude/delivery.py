from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from base.lm.harness.delivery import HarnessInput
from base.lm.harness.events import HarnessCause

# meiva:note
# Claude Code's side of the delivery contract, as pure decisions. An input is sent as a user
# message that carries a `uuid`; with `replay-user-messages` on, the CLI echoes every user message it
# takes into the conversation, with that `uuid` unchanged, at the instant the model gets it. The
# echo is therefore the second acknowledgement, and the turn it opens or joins is decided from the
# echoes and the `ResultMessage`s alone: an echo of one of our uuids with no turn open starts a
# turn (the CLI's own queued-message revival included), an echo while a turn is open was absorbed at
# a tool boundary, and a `ResultMessage` ends the open turn. An echo whose uuid is not ours (the CLI's
# own `[Request interrupted by user]`) belongs to nobody and is ignored. The behaviour was observed
# on CLI 2.1.301 (the SDK's pinned binary); `--replay-user-messages` is the one flag it rests on.
# meiva:link base/lm/harness/claude/docs/claude.md
_NAMESPACE = uuid.UUID("6f0d2c9e-3b7a-4c55-9a40-2d1f7e6b8a11")


# meiva:note
# The uuid an input is sent with. It is derived from the key, so the same key always has the same
# uuid, which is what lets a transcript be searched for an input after a crash without a stored map,
# and the CLI is always handed a well-formed uuid whatever shape the caller's key has.
def key_uuid(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, key))


def user_message(item: HarnessInput) -> dict[str, object]:
    blocks: list[dict[str, object]] = []
    if item.text.strip():
        blocks.append({"type": "text", "text": item.text})
    blocks.extend(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.mime_type,
                "data": image.data_base64,
            },
        }
        for image in item.images
    )
    content: object = item.text if not item.images else blocks
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "uuid": key_uuid(item.key),
    }


# meiva:note
# Why the CLI began a turn we did not ask for, read off the `origin` it reports. A finished background
# task is `task-notification`; the names a timer or monitor wake-up carries have no sample in the
# experiments, so any origin naming one of the scheduler words is read as a scheduled wake and every
# other origin is UNKNOWN rather than guessed.
_SCHEDULER_WORDS = ("cron", "schedule", "timer", "wake", "loop", "monitor")


def harness_cause(origin_kind: str | None) -> HarnessCause:
    if origin_kind is None:
        return HarnessCause.UNKNOWN
    if origin_kind == "task-notification":
        return HarnessCause.BACKGROUND_TASK
    if any(word in origin_kind for word in _SCHEDULER_WORDS):
        return HarnessCause.SCHEDULED_WAKE
    return HarnessCause.UNKNOWN


# meiva:note
# One turn the CLI is running, as observed. `turn_id` is the id the stream names it by and `keys` the inputs
# of ours that are in its context. A turn is reported once, under that id.
@dataclass(frozen=True, slots=True)
class OpenTurn:
    turn_id: str
    keys: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()
    steps: int = 0

    def absorbing(self, key: str) -> OpenTurn:
        return replace(self, keys=(*self.keys, key))

    def with_parts(self, texts: tuple[str, ...], *, stepped: bool) -> OpenTurn:
        return replace(self, parts=(*self.parts, *texts), steps=self.steps + (1 if stepped else 0))
