from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

SID = "conv-acp"
STAMP = "2026-11-17T05:30:00Z"
USAGE = {"inputTokens": 10, "cachedReadTokens": 30, "outputTokens": 3, "thoughtTokens": 2}
STDOUT = threading.Lock()


def frame(payload: dict[str, Any]) -> None:
    with STDOUT:
        _ = sys.stdout.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
        sys.stdout.flush()


def update(kind: str, **data: Any) -> None:
    frame(
        {
            "method": "session/update",
            "params": {
                "sessionId": SID,
                "update": {"sessionUpdate": kind, **data},
            },
        }
    )


def message(text: str) -> None:
    update("agent_message_chunk", content={"type": "text", "text": text})


def result(request: int, reason: str = "end_turn", *, usage: bool = True) -> None:
    fields: dict[str, Any] = {"stopReason": reason}
    if usage:
        fields["usage"] = USAGE
    frame({"id": request, "result": fields})


def record(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as stream:
        _ = stream.write(json.dumps(row) + "\n")


def after_release(path: Path, action: Any) -> None:
    def wait() -> None:
        while not path.exists():
            time.sleep(0.005)
        action()

    threading.Thread(target=wait, daemon=True).start()


def planner_row(text: str | None, *, tokens: int = 10) -> dict[str, object]:
    return {
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "input_tokens": tokens,
        "cache_read_tokens": 30,
        "output_tokens": 3,
        "content": text,
    }


def declare_child(transcript: Path) -> str:
    sender = "child-conv"
    descriptors = transcript.parent.parent / "subagents"
    descriptors.mkdir(exist_ok=True)
    _ = (descriptors / "child.json").write_text(json.dumps({"conversationId": sender}))
    return sender


def background_notice(transcript: Path) -> None:
    sender = declare_child(transcript) if "--test-native-child" in sys.argv else SID + "/task-1"
    record(
        transcript,
        {
            "type": "SYSTEM_MESSAGE",
            "content": f"[Message] timestamp={STAMP} sender={sender} priority=MESSAGE_PRIORITY_HIGH content=timer prompt",
        },
    )
