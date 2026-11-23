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
    if "--test-early-command-result" not in sys.argv:
        update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="WORK_DONE")
    message("ORIGINAL_DONE\n")
    message("STEER_DONE")
    result(request)


def finish_timer_steer(request: int, transcript: Path) -> None:
    if "--test-terminal-before-steer" in sys.argv:
        record(transcript, {**planner_row("STEER_DONE", tokens=25), "output_tokens": 4})
        message("STEER_DONE")
        frame(
            {
                "id": request,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {
                        **USAGE,
                        "inputTokens": 25,
                        "outputTokens": 4,
                    },
                },
            }
        )
        return
    finish_background(transcript, early_notice=True, early_planner=False)
    record(transcript, planner_row("STEER_DONE"))
    message("STEER_DONE")
    result(request)


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
    frame({"id": "collision-receipt", "method": "test/frame_barrier"})
    Path("background-sent").touch()


def trigger_background(transcript: Path) -> None:
    early_planner = "--test-background-row-first" in sys.argv
    early_notice = early_planner or "--test-early-hub" in sys.argv
    if early_notice:
        background_notice(transcript)
    if early_planner:
        record(transcript, planner_row("BACKGROUND", tokens=237))
    if "--test-four-minute-timer" in sys.argv:
        update(
            "tool_call_update",
            toolCallId="timer",
            status="completed",
            rawOutput="Timer cancelled early: its early-termination condition (any) was met by a message from user.",
        )
    else:
        update("tool_call_update", toolCallId="timer", status="completed")
    after_release(
        Path("release"), lambda: finish_background(transcript, early_notice, early_planner)
    )


def resumed_prompt(transcript: Path, request: int) -> None:
    message("REPLAYED_OLD_TEXT")
    message("MEIVA_DISPLAY_TO_USER\nOld native reply")
    update("usage_update", used=3, size=10)
    reply = "MEIVA_DISPLAY_TO_USER\nFresh reply"
    record(transcript, {**planner_row(reply, tokens=220), "created_at": STAMP})
    message(reply)
    result(request, usage=False)


def resumed_command_completion(transcript: Path, request: int, command: str) -> None:
    message("REPLAYED_OLD_TEXT")
    message("MEIVA_DISPLAY_TO_USER\nOld native reply")
    task_id = SID + "/task-2893"
    counts = (
        (7115, 308503, 1657),
        (4306, 315151, 1076),
        (2953, 319455, 495),
        (2334, 322406, 614),
        (2319, 324738, 373),
        (3395, 325008, 850),
        (5674, 328401, 600),
        (3715, 334073, 947),
        (2213, 337786, 292),
        (3479, 337950, 4220),
        (4675, 341427, 679),
    )
    unresolved = command == "unresolved-attribution"
    reply = "MEIVA_DISPLAY_TO_USER\nFresh multi-step reply"
    for index, (uncached, cached, output) in enumerate(counts):
        if index == 1 and not unresolved:
            record(
                transcript,
                {
                    "step_index": 2893,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "content": f"Created At: {STAMP}\nTool is running as a background task with task id: {task_id}\nTask Description: sanitized",
                },
            )
        if index == 6:
            record(
                transcript,
                {
                    "type": "SYSTEM_MESSAGE",
                    "content": f'[Message] timestamp={STAMP} sender={task_id} priority=MESSAGE_PRIORITY_HIGH content=Task id "{task_id}" finished with result:\nCommand exited with code 0.',
                },
            )
        final = index == len(counts) - 1
        if final and unresolved:
            reply = "AMBIGUOUS REPLY"
        row = {
            **planner_row(reply if final else None, tokens=uncached),
            "cache_read_tokens": cached,
            "output_tokens": output,
        }
        if not final:
            args = {"CommandLine": f"echo step-{index}"}
            row["tool_calls"] = [{"name": "run_command", "args": args}]
            update("tool_call", toolCallId=f"step-{index}", name="run_command", rawInput=args)
            update("tool_call_update", toolCallId=f"step-{index}", status="completed")
        record(transcript, row)
    message(reply)
    usage = {
        "inputTokens": sum(row[0] for row in counts),
        "cachedReadTokens": sum(row[1] for row in counts),
        "outputTokens": sum(row[2] for row in counts),
    }
    frame({"id": request, "result": {"stopReason": "end_turn", "usage": usage}})

    if unresolved:
        after_release(
            Path("release-native"),
            lambda: send_late_native(transcript, with_tool=Path("include-native-tool").exists()),
        )


def record_native_tool(transcript: Path, tool_id: str | None = "native-tool") -> None:
    args = {"CommandLine": "echo native"}
    record(
        transcript,
        {
            **planner_row(None, tokens=125),
            "tool_calls": [{"name": "run_command", "args": args}],
        },
    )
    if tool_id is not None:
        update("tool_call", toolCallId=tool_id, name="run_command", rawInput=args)
        update("tool_call_update", toolCallId=tool_id, status="completed")


def send_late_native(transcript: Path, *, with_tool: bool = False) -> None:
    reply = "OK" if "--test-native-same-text" in sys.argv else "LATE NATIVE REPLY"
    if with_tool:
        record_native_tool(transcript)
    if "--test-native-same-tool" in sys.argv:
        record_native_tool(transcript, None)
    record(transcript, planner_row(reply, tokens=237))
    if not Path("delay-native-frame").exists():
        message(reply)
        update("usage_update", used=2, size=10)
        frame({"id": "collision-receipt", "method": "test/frame_barrier"})
    _ = Path("native-written").write_text(str(os.getpid()))


def child_overlap(transcript: Path, request: int, command: str) -> None:
    foreground = planner_row("FOREGROUND")
    if command == "child-no-usage-incomplete":
        del foreground["output_tokens"]
    record(transcript, foreground)
    background_notice(transcript)
    deferred = command == "child-deferred"
    if not deferred:
        record(transcript, planner_row("CHILD CALLBACK", tokens=237))
    message("FOREGROUND")
    if command in {"child-late", "child-deferred"}:
        result(request)

        def deliver_child() -> None:
            if deferred:
                record(transcript, planner_row("CHILD CALLBACK", tokens=237))
            message("CHILD CALLBACK")
            update("usage_update", used=2, size=10)
            Path("child-frame-sent").touch()

        after_release(Path("release"), deliver_child)
        return
    message("CHILD CALLBACK")
    result(request, usage=not command.startswith("child-no-usage"))


def textless_prompt(transcript: Path, request: int, command: str) -> None:
    message("REPLAYED_OLD_TEXT")
    planner = planner_row("")
    if command == "textless-no-planner-usage":
        del planner["output_tokens"]
    else:
        planner["output_tokens"] = 0
    record(transcript, planner)
    frame(
        {
            "id": request,
            "result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 0},
            },
        }
    )


def cancel_prompt(transcript: Path, request: int) -> None:
    if "--test-cancel-partial" in sys.argv:
        message("REPLAYED_OLD_TEXT")
        record(transcript, planner_row("VISIBLE PARTIAL"))
        message("VISIBLE PARTIAL")
    result(request, "cancelled", usage=False)


def ambiguous_foreground(transcript: Path, request: int, *, foreground_first: bool) -> None:
    record_native_tool(transcript)
    texts = ("OK", "LATE NATIVE REPLY")
    for text in texts if foreground_first else reversed(texts):
        record(transcript, planner_row(text))
    message("O")
    message("LATE NATIVE REPLY")
    message("K")
    result(request)


def reply_with_delayed_planner(transcript: Path, request: int, timing: str) -> None:
    if timing.startswith("usage-tie-"):
        ambiguous_foreground(
            transcript, request, foreground_first=timing.endswith("-foreground-first")
        )
        return
    record_native_tool(transcript)
    record(transcript, planner_row("LATE NATIVE REPLY", tokens=237))
    if timing == "delayed-planner-early":
        message("LATE NATIVE REPLY")
    message("O")
    if timing == "delayed-planner-interleaved":
        message("LATE NATIVE REPLY")
    message("K")
    if timing == "delayed-planner-trailing":
        message("LATE NATIVE REPLY")
    result(request)
    frame({"id": "collision-receipt", "method": "test/sync", "params": {}})

    def write_foreground() -> None:
        if timing == "delayed-planner":
            message("LATE NATIVE REPLY")
        record(transcript, planner_row("OK"))
        update("usage_update", used=2, size=10)

    after_release(Path("release-foreground"), write_foreground)


def foreground_with_preamble(transcript: Path, request: int) -> None:
    record(
        transcript,
        {
            **planner_row("Working"),
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo foreground"}}],
        },
    )
    message("Working")
    record(transcript, planner_row("OK"))
    message("O")
    message("K")
    frame(
        {
            "id": request,
            "result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 20, "cachedReadTokens": 60, "outputTokens": 6},
            },
        }
    )


def fresh_prompt(transcript: Path, request: int, command: str) -> None:
    case, reason = command.split(":", 1)
    if case == "fresh-tool":
        record(
            transcript,
            {
                **planner_row(None),
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 8"}}],
            },
        )
        beginning_of_tool()
        update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="WORK_DONE")
    if case == "fresh-textless":
        record(transcript, {**planner_row(None), "output_tokens": 0})
    else:
        record(transcript, planner_row("ACTUAL_FRESH", tokens=210))
        message("ACTUAL_FRESH")
    result(request, reason, usage=False)


def plain_prompt(transcript: Path, request: int, command: str) -> None:
    if command in {"resumed-command-completion", "unresolved-attribution"}:
        resumed_command_completion(transcript, request, command)
        return
    if command == "resumed":
        resumed_prompt(transcript, request)
        return
    if command.startswith("child-") and command != "child-seed":
        child_overlap(transcript, request, command)
        return
    if command.startswith("textless"):
        textless_prompt(transcript, request, command)
        return
    followup_prompt(transcript, request, command)


def followup_prompt(transcript: Path, request: int, command: str) -> None:
    if command == "child-seed":
        _ = declare_child(transcript)
    if command == "next-with-delayed-native-frame" and (
        Path("native-written").read_text() == str(os.getpid())
    ):
        message("LATE NATIVE REPLY")
        update("usage_update", used=2, size=10)
    if command == "late-native":
        send_late_native(transcript)
    if command == "tool-then-foreground":
        record_native_tool(transcript)
    if command == "foreground-preamble":
        foreground_with_preamble(transcript, request)
        return
    if command.startswith(("delayed-planner", "usage-tie-")):
        reply_with_delayed_planner(transcript, request, command)
        return
    if command == "same-text-without-planner":
        message("OK")
        update("usage_update", used=2, size=10)
        result(request)
        after_release(Path("write-foreground"), lambda: record(transcript, planner_row("OK")))
        return
    if command == "same-tool":
        record_native_tool(transcript, "foreground-tool")
    native = command.startswith("native-")
    reply = "FOREGROUND" if native else "OK"
    record(transcript, planner_row(reply))
    message(reply)
    result(request, usage=command != "same-tool")
    if native:
        message(reply)
        update("usage_update", used=2, size=10)
        after_release(Path("release"), lambda: native_reply(transcript, command))


def native_reply(transcript: Path, command: str) -> None:
    reply = f"MEIVA_{command.removeprefix('native-')}_TO_USER\nTIMER_PURPLE"
    if command == "native-HIDE":
        reply = "MEIVA_HIDE_FROM_USER\nTIMER_PURPLE"
    background_notice(transcript)
    record(transcript, planner_row(reply, tokens=237))
    message(reply)
    update("usage_update", used=2, size=10)


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
    hold_prompt(transcript, "hold", request)
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


def hold_prompt(transcript: Path, command: str, request: int) -> None:
    if "--test-terminal-before-steer" in sys.argv:
        record(transcript, planner_row("FIRST_DONE"))
        message("FIRST_DONE")
        return
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
    if "--test-fault-held-prompt" in sys.argv:
        after_release(
            Path("release-fault"),
            lambda: frame({"id": request, "error": {"code": -32603, "message": "provider fault"}}),
        )
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


def arm_background(transcript: Path, request: int) -> None:
    params = (
        {"DurationSeconds": 240, "Prompt": "timer prompt", "TimerCondition": "any"}
        if "--test-four-minute-timer" in sys.argv
        else {"DurationSeconds": 5, "Prompt": "timer prompt"}
    )
    update("tool_call", toolCallId="timer", name="schedule", status="in_progress", rawInput=params)
    if "--test-two-timers" in sys.argv:
        update(
            "tool_call",
            toolCallId="second-timer",
            name="schedule",
            status="in_progress",
            rawInput={"DurationSeconds": 600, "Prompt": "keep checking the long-running build"},
        )
    message("TIMER_SET")
    result(request)
    after_release(Path("release-tool"), lambda: trigger_background(transcript))


def complete_steer(transcript: Path, request: int) -> None:
    if "--test-early-command-result" in sys.argv:
        update("tool_call_update", toolCallId="tool-1", status="completed", rawOutput="WORK_DONE")
    gate = "release-steer" if "--test-overlap" in sys.argv else "release"
    finish = finish_timer_steer if "--test-timer-before-steer" in sys.argv else finish_steer
    after_release(Path(gate), lambda: finish(request, transcript))


def regular_prompt(transcript: Path, request: int, command: str) -> None:
    if command.startswith("fresh-"):
        fresh_prompt(transcript, request, command)
    else:
        plain_prompt(transcript, request, command)


def handle_prompt(
    transcript: Path,
    data: dict[str, Any],
    original: int | None,
) -> tuple[int | None, int | None]:
    request = data["id"]
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
        complete_steer(transcript, request)
    # agy may omit live user_message_chunk. Emit an echo on selected paths to exercise both.
    if command in {"hold", "reactive", "background", "permission", "overlap-first"}:
        update("user_message_chunk", content={"type": "text", "text": marker})
    if command == "steer":
        return original, None
    if command in {"hold", "reactive", "hold-no-stdin"}:
        hold_prompt(transcript, command, request)
        return request, None
    permission_prompt = None
    if command == "background":
        arm_background(transcript, request)
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
        regular_prompt(transcript, request, command)
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
        if "--test-gate-load" in sys.argv:
            gate = threading.Event()
            after_release(Path("release-load"), gate.set)
            Path("load-waiting").touch()
            gate.wait()
        message("REPLAYED_OLD_TEXT")
        update("usage_update", used=1, size=10)
    frame({"id": request, "result": {"sessionId": SID} if method == "session/new" else {}})
    if method == "session/load" and "--test-late-load-replay" in sys.argv:
        message("REPLAYED_OLD_TEXT")
        message("MEIVA_DISPLAY_TO_USER\nOld native reply")
        update("usage_update", used=2, size=10)
    if method == "session/load" and "--test-timer-replay-on-load" in sys.argv:
        replay_timer_on_load()


def replay_timer_on_load() -> None:
    update(
        "tool_call",
        toolCallId="old-timer-command",
        name="run_command",
        status="completed",
        rawInput={"CommandLine": "echo old timer"},
    )
    message("LATE_TIMER")
    update("usage_update", used=3, size=10)


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
                cancel_prompt(transcript, original)
                original = None
        elif method == "session/prompt":
            original, permission_prompt = handle_prompt(transcript, data, original)
        elif request == "collision-receipt":
            Path("collision-receipt-read").touch()
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
