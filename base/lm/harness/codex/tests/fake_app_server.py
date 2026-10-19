from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from base.lm.harness.codex.transport import (
    ClosedHandler,
    JsonObject,
    JsonRpcError,
    NotificationHandler,
    RequestHandler,
    RequestParams,
)

CODEX_PID = 4242


# meiva:note
# An in-process app-server with the same transport surface and scripted replies.
# The test pushes notifications through the helpers below, so it controls exactly
# where in the turn each one lands.
class _Reapable(Protocol):
    def reap(self, *pids: int) -> None: ...


class FakeAppServer:
    def __init__(
        self,
        *,
        thread_id: str = "thread-1",
        pid: int | None = CODEX_PID,
        resume_fails: bool = False,
        resume_cwd: str = "workspace",
    ) -> None:
        self.thread_id = thread_id
        self._pid = pid
        self.probe: _Reapable | None = None
        self.resume_fails = resume_fails
        self.resume_cwd = resume_cwd
        self.started = False
        self.closed = False
        self.requests: list[tuple[str, RequestParams]] = []
        self.notifications: list[tuple[str, RequestParams | None]] = []
        self.turn_ids: list[str] = []
        self.on_interrupt: Callable[[], None] | None = None
        self._notification_handler: NotificationHandler | None = None
        self._request_handler: RequestHandler | None = None
        self._closed_handler: Any = None

    @property
    def pid(self) -> int | None:
        return self._pid

    def on_notification(self, handler: NotificationHandler | None) -> None:
        self._notification_handler = handler

    def on_request(self, handler: RequestHandler | None) -> None:
        self._request_handler = handler

    def on_closed(self, handler: ClosedHandler | None) -> None:
        self._closed_handler = handler

    async def start(self) -> None:
        self.started = True

    async def request(
        self, method: str, params: RequestParams, *, timeout: float | None = None
    ) -> JsonObject:
        self.requests.append((method, params))
        if method == "initialize":
            return {"userAgent": "fake-app-server"}
        if method == "thread/start":
            return {
                "thread": {"id": self.thread_id, "cwd": params.get("cwd")},
                "model": "gpt-fake",
            }
        if method == "thread/resume":
            if self.resume_fails:
                raise JsonRpcError(-32603, "thread not found")
            return {
                "thread": {"id": params["threadId"], "cwd": self.resume_cwd},
                "model": "gpt-fake",
            }
        if method == "turn/start":
            turn_id = f"codex-turn-{len(self.turn_ids) + 1}"
            self.turn_ids.append(turn_id)
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        if method == "turn/interrupt":
            if self.on_interrupt is not None:
                self.on_interrupt()
            return {}
        return {}

    async def notify(self, method: str, params: RequestParams | None = None) -> None:
        self.notifications.append((method, params))

    async def close(self) -> None:
        self.closed = True
        # meiva:note
        # The real app-server process ends with the transport; a fake that stayed
        # "alive" would make every close test pay the adapter's settle window and stop
        # modelling the fact the close path checks.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
        if self.probe is not None and self._pid is not None:
            self.probe.reap(self._pid)
        if self._closed_handler is not None:
            self._closed_handler("closed by client")

    async def ask(self, method: str, params: JsonObject) -> JsonObject:
        assert self._request_handler is not None, "session registered no request handler"
        return await self._request_handler(method, params)

    def emit(self, method: str, params: JsonObject) -> None:
        assert self._notification_handler is not None, "session registered no notification handler"
        self._notification_handler(method, params)

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def params_for(self, method: str) -> list[RequestParams]:
        return [params for called, params in self.requests if called == method]

    @property
    def current_turn(self) -> str:
        return self.turn_ids[-1]

    def delta(self, text: str, *, turn_id: str | None = None) -> None:
        self.emit(
            "item/agentMessage/delta",
            {
                "threadId": self.thread_id,
                "turnId": turn_id or self.current_turn,
                "itemId": "msg-1",
                "delta": text,
            },
        )

    def message(
        self, text: str, *, phase: str = "final_answer", turn_id: str | None = None
    ) -> None:
        self.emit(
            "item/completed",
            {
                "threadId": self.thread_id,
                "turnId": turn_id or self.current_turn,
                "completedAtMs": 0,
                "item": {"type": "agentMessage", "id": "msg-1", "text": text, "phase": phase},
            },
        )

    def command_started(
        self,
        *,
        item_id: str = "exec-1",
        command: str = "echo hi",
        cwd: str = "workspace",
        turn_id: str | None = None,
    ) -> None:
        self.emit(
            "item/started",
            {
                "threadId": self.thread_id,
                "turnId": turn_id or self.current_turn,
                "item": {
                    "type": "commandExecution",
                    "id": item_id,
                    "command": command,
                    "cwd": cwd,
                    "status": "inProgress",
                },
            },
        )

    def command_completed(
        self,
        *,
        item_id: str = "exec-1",
        exit_code: int = 0,
        output: str = "hi",
        status: str = "completed",
        turn_id: str | None = None,
    ) -> None:
        self.emit(
            "item/completed",
            {
                "threadId": self.thread_id,
                "turnId": turn_id or self.current_turn,
                "item": {
                    "type": "commandExecution",
                    "id": item_id,
                    "command": "echo hi",
                    "cwd": "workspace",
                    "status": status,
                    "exitCode": exit_code,
                    "aggregatedOutput": output,
                },
            },
        )

    def usage(
        self,
        *,
        input_tokens: int = 10,
        output_tokens: int = 5,
        cached_input_tokens: int = 0,
        turn_id: str | None = None,
    ) -> None:
        self.emit(
            "thread/tokenUsage/updated",
            {
                "threadId": self.thread_id,
                "turnId": turn_id or self.current_turn,
                "tokenUsage": {
                    "total": {"inputTokens": input_tokens, "outputTokens": output_tokens},
                    "last": {
                        "inputTokens": input_tokens,
                        "cachedInputTokens": cached_input_tokens,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": output_tokens,
                        "reasoningOutputTokens": 0,
                        "totalTokens": input_tokens + output_tokens,
                    },
                    "modelContextWindow": 200000,
                },
            },
        )

    def complete(
        self,
        *,
        status: str = "completed",
        error: str | None = None,
        duration_ms: int = 12,
        turn_id: str | None = None,
    ) -> None:
        turn: dict[str, Any] = {
            "id": turn_id or self.current_turn,
            "status": status,
            "durationMs": duration_ms,
            "items": [],
        }
        if error is not None:
            turn["error"] = {"message": error}
        self.emit("turn/completed", {"threadId": self.thread_id, "turn": turn})
