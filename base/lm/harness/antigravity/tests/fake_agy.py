from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast


# meiva:note
# A stand-in for the `agy` CLI, speaking its NDJSON stream on stdin/stdout. The
# adapter is tested against the real framing, not against a Python object: this
# script is spawned exactly like `agy`, reads `{"event":"user",...}` lines and
# writes `init` / `step_update` / `result`. `FAKE_AGY_PLAN` names a JSON plan;
# every turn in it is one entry consumed per inbound message.
class _InterruptedError(Exception):
    pass


def _on_sigint(signum: int, frame: object) -> None:
    raise _InterruptedError


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


# meiva:note
# The tests poll these paths every 5ms, so the path itself is a reader's only signal.
# Opening for append created the file before its content landed, and a buffered write
# becomes visible only at close, so a reader could see an empty or half-written line
# and `splitlines()[0]` raised IndexError. Replacing the path keeps it either absent
# or complete. One writer per path: each harness owns its own files.
def _record(path: str | None, value: object) -> None:
    if not path:
        return
    target = Path(path)
    pending = target.with_name(target.name + ".pending")
    written = target.read_bytes() if target.exists() else b""
    pending.write_bytes(written + (json.dumps(value) + "\n").encode())
    pending.replace(target)


def _result(conversation: str, **fields: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "conversation_id": conversation,
        "status": "SUCCESS",
        "response": "",
        "duration_seconds": 0.01,
        "num_turns": 1,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 12,
        },
    }
    payload.update(fields)
    return {"event": "result", "result": payload}


def _turn_at(turns: list[dict[str, Any]], cursor_path: str | None) -> dict[str, Any]:
    index = 0
    if cursor_path:
        cursor = Path(cursor_path)
        if cursor.exists():
            index = int(cursor.read_text(encoding="utf-8") or 0)
        cursor.write_text(str(index + 1), encoding="utf-8")
    return turns[index] if index < len(turns) else {"steps": [], "result": {}}


def _spawn_child(turn: dict[str, Any]) -> subprocess.Popen[bytes] | None:
    if not turn.get("spawn_child"):
        return None
    child = subprocess.Popen(["sleep", "30"])
    _record(os.environ.get("FAKE_AGY_CHILD"), child.pid)
    return child


def _emit_steps(conversation: str, turn: dict[str, Any]) -> None:
    steps = cast("list[dict[str, Any]]", turn.get("steps") or [])
    for index, step in enumerate(steps, start=1):
        _emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation,
                    "step_index": step.pop("step_index", index),
                    **step,
                },
            }
        )
        time.sleep(float(turn.get("pace", 0.0)))


def _hang(conversation: str) -> None:
    _emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conversation,
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": "working",
            },
        }
    )
    while True:
        time.sleep(0.05)


def main() -> int:
    plan_path = os.environ.get("FAKE_AGY_PLAN")
    plan = cast(
        "dict[str, Any]",
        json.loads(Path(plan_path).read_text(encoding="utf-8")) if plan_path else {"turns": []},
    )
    received = os.environ.get("FAKE_AGY_RECEIVED")
    cursor_path = os.environ.get("FAKE_AGY_CURSOR")
    _record(os.environ.get("FAKE_AGY_ARGV"), sys.argv[1:])

    conversation = plan.get("conversation_id") or "conv-fake"
    _emit(
        {
            "event": "init",
            "conversation_id": conversation,
            "init": {
                "model": plan.get("model", "gemini-4-argon-high"),
                "cwd": str(Path.cwd()),
                "permission_mode": "always-proceed",
            },
        }
    )

    signal.signal(signal.SIGINT, _on_sigint)
    turns = list(cast("list[dict[str, Any]]", plan.get("turns") or []))
    child: subprocess.Popen[bytes] | None = None
    turn: dict[str, Any] = {}
    try:
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            content = json.loads(text).get("message", {}).get("content", "")
            _record(received, content)
            turn = _turn_at(turns, cursor_path)
            child = _spawn_child(turn)
            if turn.get("hang"):
                _hang(conversation)
            _emit_steps(conversation, turn)
            _emit(_result(conversation, **cast("dict[str, Any]", turn.get("result") or {})))
    except _InterruptedError:
        if child is not None and turn.get("kill_child_on_interrupt"):
            child.terminate()
            child.wait(timeout=5)
        _emit(_result(conversation, status="ERROR", error="interrupted"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
