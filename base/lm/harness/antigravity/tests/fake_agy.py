from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

_RECORD_LOCK = threading.Lock()


# meiva:note
# The conversation state the in-process RPC reports, as agy 1.5.5 does: `status` is RUNNING while a
# turn runs and IDLE otherwise, `steps` is the trajectory (user-input steps carry the
# `clientMessageId` of a message sent through RPC), and `replay` holds the step frames of a phantom turn
# (an input sent while IDLE: agy runs it, prints nothing, and prints its steps at the next stdin input).
class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "CASCADE_RUN_STATUS_IDLE"
        self.steps: list[dict[str, Any]] = []
        self.replay: list[dict[str, Any]] = []
        # meiva:note
        # How many `GetCascadeTrajectory` answers still say RUNNING although the turn ended: the
        # moment between agy finishing a turn and a caller reading it, in which an input sent through
        # RPC starts a phantom turn.
        self.turns_done = 0
        self.stale_calls = int(os.environ.get("FAKE_AGY_STALE_CALLS") or 0)

    def add_step(self, kind: str, client_id: str | None = None) -> int:
        with self.lock:
            step: dict[str, Any] = {"type": kind, "status": "CORTEX_STEP_STATUS_DONE"}
            if client_id is not None:
                step["userInput"] = {"clientMessageId": client_id}
            self.steps.append(step)
            return len(self.steps) - 1

    def note_index(self, index: int) -> None:
        with self.lock:
            while len(self.steps) <= index:
                self.steps.append({"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE"})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "trajectory": {"steps": list(self.steps)},
                "status": self.status,
                "numTotalSteps": len(self.steps),
            }


_STATE = _State()


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
    with _RECORD_LOCK:
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


def _emit_steps(
    conversation: str, turn: dict[str, Any], *, key: str = "steps", start_index: int = 1
) -> int:
    steps = cast("list[dict[str, Any]]", turn.get(key) or [])
    next_index = start_index
    for offset, raw_step in enumerate(steps):
        step = dict(raw_step)
        step_index = int(step.pop("step_index", start_index + offset))
        next_index = max(next_index, step_index + 1)
        _emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation,
                    "step_index": step_index,
                    **step,
                },
            }
        )
        _STATE.note_index(step_index)
        time.sleep(float(turn.get("pace", 0.0)))
    return next_index


def _run_phantom(user_index: int, text: str) -> None:
    _STATE.status = "CASCADE_RUN_STATUS_RUNNING"
    pause = float(os.environ.get("FAKE_AGY_PHANTOM_SECONDS") or 0.2)

    def finish() -> None:
        time.sleep(pause)
        answer = f"phantom answer to {text}"
        reply_index = _STATE.add_step("CORTEX_STEP_TYPE_PLANNER_RESPONSE")
        transcript = os.environ.get("FAKE_AGY_TRANSCRIPT")
        if transcript:
            _record(
                transcript,
                {
                    "step_index": reply_index,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": answer,
                },
            )
        with _STATE.lock:
            _STATE.replay = [
                {"step_index": user_index, "state": "DONE", "step_type": "user_input"},
                {
                    "step_index": reply_index,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": answer,
                },
            ]
            _STATE.status = "CASCADE_RUN_STATUS_IDLE"

    threading.Thread(target=finish, daemon=True).start()


def _message_text(payload: dict[str, Any]) -> str:
    items = cast("list[dict[str, Any]]", payload.get("items") or [])
    return str(items[0].get("text") or "") if items else ""


def _trajectory_reply() -> dict[str, Any]:
    if os.environ.get("FAKE_AGY_BAD_TRAJECTORY"):
        return {"unexpected": True}
    snap = _STATE.snapshot()
    if _STATE.turns_done > 0 and _STATE.stale_calls > 0:
        _STATE.stale_calls -= 1
        snap["status"] = "CASCADE_RUN_STATUS_RUNNING"
    return snap


def _accept_message(payload: dict[str, Any], received_path: str | None) -> None:
    text = _message_text(payload)
    _record(received_path, text)
    _record(os.environ.get("FAKE_AGY_RPC"), payload)
    client_id = payload.get("clientMessageId")
    index = _STATE.add_step(
        "CORTEX_STEP_TYPE_USER_INPUT", client_id if isinstance(client_id, str) else None
    )
    if _STATE.status == "CASCADE_RUN_STATUS_IDLE":
        _run_phantom(index, text)


def _start_rpc_server(
    csrf_token: str,
    received_path: str | None,
    steered: queue.Queue[str],
) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if csrf_token and self.headers.get("x-codeium-csrf-token") != csrf_token:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"message":"invalid csrf token"}')
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            payload = cast("dict[str, Any]", json.loads(raw) if raw.strip() else {})
            if self.path.endswith("/GetWorkspaceInfos"):
                self._respond(200, {"workspaceInfos": []})
                return
            if self.path.endswith("/GetCascadeTrajectory"):
                self._respond(200, _trajectory_reply())
                return
            if self.path.endswith("/SendUserCascadeMessage"):
                _accept_message(payload, received_path)
                self._respond(200, {})
                steered.put(_message_text(payload))
                return
            self._respond(404, {"message": f"unknown path: {self.path}"})

        def _respond(self, status: int, body: dict[str, object]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _emit_replay(conversation: str) -> None:
    for frame in _take_replay():
        _emit({"event": "step_update", "step_update": {"conversation_id": conversation, **frame}})


def _take_replay() -> list[dict[str, Any]]:
    with _STATE.lock:
        frames, _STATE.replay = _STATE.replay, []
    return frames


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


def _emit_raw_stream(plan: dict[str, object]) -> bool:
    raw_stream = plan.get("raw_stream")
    if not isinstance(raw_stream, dict):
        return False
    stream_spec = cast("dict[str, object]", raw_stream)
    conversation = str(plan.get("conversation_id") or "conv-fake")
    _emit({"event": "init", "conversation_id": conversation})
    raw_blob = stream_spec.get("blob_bytes")
    blob_bytes = raw_blob if isinstance(raw_blob, int) else 0
    if blob_bytes:
        _emit({"event": "step_update", "blob": "x" * blob_bytes})
    raw_eof = stream_spec.get("trailing_eof_bytes")
    eof_bytes = raw_eof if isinstance(raw_eof, int) else 0
    if eof_bytes:
        _ = sys.stdout.write(json.dumps({"event": "result", "blob": "y" * eof_bytes}))
        sys.stdout.flush()
    return True


def _serve_plan() -> int:
    plan_path = os.environ.get("FAKE_AGY_PLAN")
    if plan_path:
        loaded = cast("dict[str, object]", json.loads(Path(plan_path).read_text(encoding="utf-8")))
        if _emit_raw_stream(loaded):
            return 0
    return main()


def main() -> int:
    plan_path = os.environ.get("FAKE_AGY_PLAN")
    plan = cast(
        "dict[str, Any]",
        json.loads(Path(plan_path).read_text(encoding="utf-8")) if plan_path else {"turns": []},
    )
    received = os.environ.get("FAKE_AGY_RECEIVED")
    cursor_path = os.environ.get("FAKE_AGY_CURSOR")
    csrf_token = os.environ.get("ANTIGRAVITY_SERVER_CSRF_TOKEN") or ""
    steered: queue.Queue[str] = queue.Queue()
    rpc_server = _start_rpc_server(csrf_token, received, steered)
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
            _emit_replay(conversation)
            _STATE.status = "CASCADE_RUN_STATUS_RUNNING"
            _ = _STATE.add_step("CORTEX_STEP_TYPE_USER_INPUT")
            turn = _turn_at(turns, cursor_path)
            child = _spawn_child(turn)
            if turn.get("hang"):
                _hang(conversation)
            next_index = _emit_steps(conversation, turn)
            if turn.get("wait_for_steer"):
                _ = steered.get(timeout=float(turn.get("steer_timeout", 5.0)))
            if turn.get("steered_steps"):
                _ = _emit_steps(conversation, turn, key="steered_steps", start_index=next_index)
            _STATE.status = "CASCADE_RUN_STATUS_IDLE"
            _STATE.turns_done += 1
            time.sleep(float(turn.get("idle_before_result", 0.0)))
            _emit(_result(conversation, **cast("dict[str, Any]", turn.get("result") or {})))
    except _InterruptedError:
        if child is not None and turn.get("kill_child_on_interrupt"):
            child.terminate()
            child.wait(timeout=5)
        _emit(_result(conversation, status="ERROR", error="interrupted"))
        return 1
    finally:
        rpc_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(_serve_plan())
