"""A stdio ACP peer with explicit release gates for asynchronous provider events."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class LocalRPC(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.headers.get("X-Codeium-Csrf-Token") != os.environ.get(
            "ANTIGRAVITY_SERVER_CSRF_TOKEN"
        ):
            self.send_error(403)
            return
        if self.path.endswith("/GetCascadeTrajectory"):
            fixture = Path("trajectory.json")
            payload = (
                fixture.read_bytes()
                if fixture.exists()
                else b'{"status":"CASCADE_RUN_STATUS_IDLE","trajectory":{}}'
            )
        else:
            payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        _ = self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


def finish_steer(request: int) -> None:
    update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="WORK_DONE")
    message("ORIGINAL_DONE\n")
    message("STEER_DONE")
    result(request)


def finish_background() -> None:
    update("tool_call_update", toolCallId="timer", status="completed")
    message("BACK")
    message("GROUND")
    update("usage_update", used=50, size=1000)


def handle_prompt(
    transcript: Path,
    data: dict[str, Any],
    original: int | None,
) -> tuple[int | None, int | None]:
    request = data["id"]
    permission_prompt = None
    text = data["params"]["prompt"][0]["text"]
    marker, _, command = text.partition("\n")
    if command == "steer":
        assert original is not None
        result(original)
        original = None
        after_release(Path("release"), lambda request=request: finish_steer(request))
    record(
        transcript,
        {"type": "USER_INPUT", "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"},
    )
    # agy may omit live user_message_chunk. Emit an echo on selected paths to exercise both.
    if command in {"hold", "reactive", "background", "permission"}:
        update("user_message_chunk", content={"type": "text", "text": marker})
    if command in {"hold", "reactive", "steer"}:
        if command != "steer":
            original = request
            update(
                "tool_call",
                toolCallId="tool-1",
                name="run_command",
                status="in_progress",
                rawInput={"CommandLine": "sleep 8"},
            )
        if command == "reactive":
            message("Parked until the tool completes")
            update("usage_update", used=50, size=1000)
        return original, permission_prompt
    if command == "background":
        update(
            "tool_call",
            toolCallId="timer",
            name="schedule",
            status="in_progress",
            rawInput={"DurationSeconds": 5},
        )
        message("TIMER_SET")
        result(request)
        after_release(Path("release"), finish_background)
    elif command == "permission":
        permission_prompt = request
        update("tool_call", toolCallId="denied", name="run_command", status="pending")
        frame(
            {
                "id": "permission-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": SID,
                    "toolCall": {"title": "Run command"},
                    "options": [{"kind": "reject_once", "optionId": "deny"}],
                },
            }
        )
    elif command == "broken":
        frame({"id": request, "error": {"code": -32603, "message": "provider error"}})
    else:
        message("OK")
        result(request)
    return original, permission_prompt


def raw_peer(*, version: str, large_frames: bool) -> None:
    for _ in range(3 if large_frames else 1):
        request = json.loads(input())
        frame(
            {
                "id": request["id"],
                "result": {
                    "protocolVersion": 1,
                    "sessionId": "conv-frames",
                    "agentInfo": {"version": version},
                },
            }
        )
    if large_frames:
        frame({"blob": "x" * (10 * 1024**2)})
        _ = sys.stdout.write(json.dumps({"jsonrpc": "2.0", "tail": "end"}))
        sys.stdout.flush()


def main() -> None:
    home = Path(os.environ["HOME"])
    transcript = (
        home
        / ".gemini/antigravity-cli/brain"
        / SID
        / ".system_generated/logs/transcript_full.jsonl"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    peer = ThreadingHTTPServer(("127.0.0.1", 0), LocalRPC)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    original = None
    permission_prompt = None
    for line in sys.stdin:
        data = json.loads(line)
        record(Path("frames.jsonl"), data)
        method = data.get("method")
        request = data.get("id")
        if method == "initialize":
            frame(
                {
                    "id": request,
                    "result": {"protocolVersion": 1, "agentInfo": {"version": "1.5.14"}},
                }
            )
        elif method in ("session/new", "session/load"):
            if method == "session/load":
                message("REPLAYED_OLD_TEXT")
                update("usage_update", used=1, size=10)
            frame({"id": request, "result": {"sessionId": SID} if method == "session/new" else {}})
        elif method == "session/set_model":
            frame({"id": request, "result": {}})
        elif method == "session/cancel":
            if original is not None:
                result(original, "cancelled", usage=False)
                original = None
        elif method == "session/prompt":
            original, permission_prompt = handle_prompt(transcript, data, original)
        elif request == "permission-1":
            assert permission_prompt is not None
            assert data["result"]["outcome"] == {"outcome": "selected", "optionId": "deny"}
            update("tool_call_update", toolCallId="denied", status="failed")
            result(permission_prompt)


if __name__ == "__main__":
    if "--test-legacy" in sys.argv:
        raw_peer(version="1.5.6", large_frames=False)
    elif "--test-large-frames" in sys.argv:
        raw_peer(version="1.5.14", large_frames=True)
    else:
        main()
