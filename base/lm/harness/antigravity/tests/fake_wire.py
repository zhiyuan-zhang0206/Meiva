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
