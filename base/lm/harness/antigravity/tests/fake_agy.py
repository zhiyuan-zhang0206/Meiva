"""A stdio ACP peer with explicit release gates for asynchronous provider events."""

from __future__ import annotations

import json
import os
import subprocess
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


def planner_row(text: str | None, *, tokens: int = 10) -> dict[str, object]:
    return {
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "input_tokens": tokens,
        "cache_read_tokens": 30,
        "output_tokens": 3,
        "content": text,
    }


def finish_steer(request: int, transcript: Path) -> None:
    record(transcript, planner_row("ORIGINAL_DONE\nSTEER_DONE"))
    update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="WORK_DONE")
    message("ORIGINAL_DONE\n")
    message("STEER_DONE")
    result(request)


def background_notice(transcript: Path) -> None:
    record(
        transcript,
        {
            "type": "SYSTEM_MESSAGE",
            "content": f"[Message] sender={SID}/task-1 priority=MESSAGE_PRIORITY_HIGH content=timer prompt",
        },
    )


def finish_background(transcript: Path, early_notice: bool, early_planner: bool) -> None:
    if not early_notice:
        background_notice(transcript)
    missing_tail = "--test-missing-background-planner" in sys.argv
    if not early_planner and not missing_tail:
        record(transcript, planner_row("BACKGROUND", tokens=237))
    if "--test-interleaved-chunks" in sys.argv:
        message("BACKGROUND")
        message("GROUND")
    else:
        message("BACK")
        if not missing_tail:
            message("GROUND")
    Path("background-sent").touch()


def trigger_background(transcript: Path) -> None:
    early_planner = "--test-background-row-first" in sys.argv
    early_notice = early_planner or "--test-early-hub" in sys.argv
    if early_notice:
        background_notice(transcript)
    if early_planner:
        record(transcript, planner_row("BACKGROUND", tokens=237))
    update("tool_call_update", toolCallId="timer", status="completed")
    after_release(
        Path("release"), lambda: finish_background(transcript, early_notice, early_planner)
    )


def beginning_of_tool() -> None:
    update(
        "tool_call",
        toolCallId="tool-1",
        name="run_command",
        status="in_progress",
        rawInput={"CommandLine": "sleep 8"},
    )


def foreground_after_cancel(request: int) -> None:
    update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="STALE")
    update("tool_call", toolCallId="tool-2", name="run_command", status="completed")
    message("OK")
    result(request)


def foreground_multiple(transcript: Path, request: int) -> None:
    record(transcript, planner_row("FIRST"))
    message("FIRST")
    update("usage_update", used=10, size=1000)

    def finish() -> None:
        record(transcript, planner_row("TRUE FINAL", tokens=20))
        message("TRUE FINAL")
        frame(
            {
                "id": request,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {
                        "inputTokens": 30,
                        "cachedReadTokens": 60,
                        "outputTokens": 6,
                    },
                },
            }
        )
        Path("front-result-sent").touch()

    after_release(Path("release-front-result"), finish)


def foreground_first(transcript: Path, request: int) -> None:
    if "--test-multi-planner" in sys.argv:
        foreground_multiple(transcript, request)
        return
    hold_prompt(transcript, "hold")
    update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="WORK_DONE")
    record(transcript, planner_row("FOREGROUND"))
    split = {"--test-interleaved-chunks", "--test-truncated-foreground"}
    message("FORE" if split.intersection(sys.argv) else "FOREGROUND")
    update("usage_update", used=50, size=1000)
    Path("foreground-sent").touch()
    after_release(
        Path("release-front-result"),
        lambda: frame(
            {
                "id": request,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 20, "cachedReadTokens": 60, "outputTokens": 6},
                },
            }
        ),
    )


def hold_prompt(transcript: Path, command: str) -> None:
    if "--test-child" in sys.argv:
        child = subprocess.Popen(("/bin/sleep", "120"), start_new_session=True)
        _ = Path("child.pid").write_text(str(child.pid))
    record(
        transcript,
        {
            **planner_row(None),
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 8"}}],
        },
    )
    beginning_of_tool()
    if command == "reactive":
        message("Parked until the tool completes")
        update("usage_update", used=50, size=1000)
    if command == "hold-no-stdin":
        threading.Event().wait()


def exit_peer() -> None:
    if "--test-child" in sys.argv:
        child = subprocess.Popen(("/bin/sleep", "120"), start_new_session=True)
        _ = Path("child.pid").write_text(str(child.pid))
    sys.exit(0)


def handle_prompt(
    transcript: Path,
    data: dict[str, Any],
    original: int | None,
) -> tuple[int | None, int | None]:
    request = data["id"]
    permission_prompt = None
    text = data["params"]["prompt"][0]["text"]
    marker, _, command = text.partition("\n")
    record(
        transcript,
        {"type": "USER_INPUT", "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"},
    )
    if command == "steer":
        assert original is not None
        result(original)
        original = None
        gate = "release-steer" if "--test-overlap" in sys.argv else "release"
        after_release(Path(gate), lambda: finish_steer(request, transcript))
    # agy may omit live user_message_chunk. Emit an echo on selected paths to exercise both.
    if command in {"hold", "reactive", "background", "permission", "overlap-first"}:
        update("user_message_chunk", content={"type": "text", "text": marker})
    if command == "steer":
        return original, permission_prompt
    if command in {"hold", "reactive", "hold-no-stdin"}:
        hold_prompt(transcript, command)
        return request, permission_prompt
    if command == "background":
        update(
            "tool_call",
            toolCallId="timer",
            name="schedule",
            status="in_progress",
            rawInput={"DurationSeconds": 5, "Prompt": "timer prompt"},
        )
        message("TIMER_SET")
        result(request)
        after_release(Path("release-tool"), lambda: trigger_background(transcript))
    elif command == "overlap-first":
        after_release(Path("release-foreground"), lambda: foreground_first(transcript, request))
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
    elif command == "after-cancel":
        foreground_after_cancel(request)
    elif command == "eof":
        exit_peer()
    elif command == "noisy-usage":
        record(transcript, {"type": "SYSTEM_MESSAGE", "content": "[Message] sender=conv/task-1"})
        record(transcript, planner_row("unrelated background", tokens=2000))
        record(transcript, planner_row("incomplete foreground", tokens=1))
        message("OK")
        result(request)
    elif command == "broken":
        frame({"id": request, "error": {"code": -32603, "message": "provider error"}})
    else:
        record(transcript, planner_row("OK"))
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


def setup_session(method: str, request: int) -> None:
    if method == "session/load":
        if "--test-stall-load" in sys.argv:
            threading.Event().wait()
        message("REPLAYED_OLD_TEXT")
        update("usage_update", used=1, size=10)
    frame({"id": request, "result": {"sessionId": SID} if method == "session/new" else {}})
    if method == "session/load" and "--test-live-load" in sys.argv:
        message("LIVE_AFTER_LOAD")
        update("usage_update", used=2, size=10)


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
            setup_session(method, request)
        elif method == "session/set_model":
            frame({"id": request, "result": {}})
        elif method == "session/cancel":
            if original is not None and "--test-ignore-cancel" not in sys.argv:
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
    if "--test-no-acp" in sys.argv:
        sys.stderr.write("unknown command acp\n")
        sys.exit(2)
    elif "--test-legacy" in sys.argv:
        raw_peer(version="1.5.6", large_frames=False)
    elif "--test-large-frames" in sys.argv:
        raw_peer(version="1.5.14", large_frames=True)
    else:
        main()
