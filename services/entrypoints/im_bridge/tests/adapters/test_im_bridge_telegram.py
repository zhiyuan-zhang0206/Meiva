"""`services.entrypoints.im_bridge.adapters.telegram` — poll wiring, tap handling, edits.

Regression guard: inline-keyboard taps arrive as ``callback_query`` updates —
``allowed_updates`` must list them or Telegram silently drops every button
press (the /list tap-to-switch card was dead in prod, 2026-08-03).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from services.entrypoints.im_bridge.adapters.telegram import TelegramAdapter
from services.entrypoints.im_bridge.tests.slices import telegram_config

FAKE_BOT_TOKEN = "123456:TEST-TOKEN"  # noqa: S105 - test fixture, never a real secret
OWNER = 123456789


class _FakeCore:
    """Records what the adapter hands over; replies immediately."""

    def __init__(self) -> None:
        self.inbound: list[tuple[str, str, str]] = []  # (channel, chat_id, text)

    async def handle_inbound(self, msg: Any) -> None:
        self.inbound.append((msg.channel, msg.chat_id, msg.text))


def _adapter(core: _FakeCore, handler: Any, state_dir: Path, **config: Any) -> TelegramAdapter:
    slice_ = telegram_config(telegram_bot_token=FAKE_BOT_TOKEN, telegram_owner_id=OWNER, **config)
    return TelegramAdapter(
        core,
        slice_,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        state_dir=state_dir,
    )


def test_get_updates_requests_callback_query(state_dir: Path) -> None:
    """The poll asks Telegram for message AND callback_query updates — without
    the latter, inline-keyboard taps never arrive (prod bug 2026-08-03)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "result": []})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler, state_dir)
        await adapter._get_updates()
        await adapter._http.aclose()

    asyncio.run(scenario())
    allowed = json.loads(seen["params"]["allowed_updates"])
    assert allowed == ["message", "callback_query"]


def test_get_updates_uses_configured_poll_timeout(state_dir: Path) -> None:
    """The long-poll window rides MEIVA_TELEGRAM_POLL_TIMEOUT_SECONDS (task
    #698 G8), and the HTTP read timeout stays 10s above it."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout_param"] = request.url.params.get("timeout")
        return httpx.Response(200, json={"ok": True, "result": []})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler, state_dir, telegram_poll_timeout_seconds=33)
        await adapter._get_updates()
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert seen["timeout_param"] == "33"


def test_callback_tap_runs_command_and_acks(state_dir: Path) -> None:
    """A button tap becomes a command in the core, then the tap is acked so
    Telegram clears the spinner."""
    core = _FakeCore()
    answered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            answered.append(json.loads(request.content)["callback_query_id"])
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected call: {request.url}")

    update = {
        "update_id": 42,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": OWNER},
            "message": {
                "message_id": 77,
                "chat": {"id": OWNER},
                "text": "Live agents",
            },
            "data": "/switch 405",
        },
    }

    async def scenario() -> None:
        adapter = _adapter(core, handler, state_dir)
        await adapter._handle_update(update)
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert core.inbound == [("telegram", str(OWNER), "/switch 405")]
    assert answered == ["cq-1"]


def test_callback_from_stranger_consumed(state_dir: Path) -> None:
    core = _FakeCore()
    answered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            answered.append("called")
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected call: {request.url}")

    update = {
        "update_id": 43,
        "callback_query": {
            "id": "cq-2",
            "from": {"id": 999},
            "message": {"message_id": 78, "chat": {"id": 999}},
            "data": "/switch 405",
        },
    }

    async def scenario() -> None:
        adapter = _adapter(core, handler, state_dir)
        await adapter._handle_update(update)
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert core.inbound == []  # stranger taps are consumed, never surfaced
    assert answered == []  # and never acked


def test_send_posts_html_message(state_dir: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1234}})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler, state_dir)
        await adapter.send(str(OWNER), "hello")
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert captured["payload"]["text"] == "hello"
    assert captured["payload"]["parse_mode"] == "HTML"  # send always renders escaped HTML


def test_typing_sends_chat_action(state_dir: Path) -> None:
    """The native processing indicator: sendChatAction with action=typing —
    Telegram shows the bouncing dots, no message is sent."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler, state_dir)
        await adapter.typing(str(OWNER))
        await adapter._http.aclose()

    asyncio.run(scenario())
    assert captured["payload"] == {"chat_id": str(OWNER), "action": "typing"}


def test_command_menu_includes_spawn_and_commands(state_dir: Path) -> None:
    """The persistent command menu registers the full command set — tapping
    one autofills the input box (Telegram native behavior)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async def scenario() -> None:
        adapter = _adapter(_FakeCore(), handler, state_dir)
        await adapter._install_command_menu()
        await adapter._http.aclose()

    asyncio.run(scenario())
    commands = [c["command"] for c in captured["payload"]["commands"]]
    # /switch is gone from the frontend: /list buttons switch (user ruling 2026-08-04)
    assert commands == ["list", "spawn", "status", "commands", "help"]
