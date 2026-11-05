# meiva:link services/docs/gateway_side/im_bridge/im_bridge.meiva.okf.md

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict

from psycopg_pool import ConnectionPool
from pydantic import TypeAdapter

from base.api_contracts.idempotency import keyed_headers
from base.api_contracts.notice_question import parse_question, question_instructions
from base.clock import Clock
from base.clock.ids import new_uuid
from base.db.rows import query_all
from base.db.transaction import write_transaction
from services.entrypoints.im_bridge import copy
from services.entrypoints.im_bridge.config import ImBridgeConfig
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.types import (
    IMAdapter,
    NoticeBridgeOwner,
)

_log = logging.getLogger("services.entrypoints.im_bridge.notice_bridge")

_CB_REPLY = "notice:reply:"
_CB_READ = "notice:read:"
_CB_DISMISS = "notice:dismiss:"
_CB_LIST = "notice:list"


# Direct-DB reads (R3 door ④, decision 2): the notice bridge reads
# agent_notices itself instead of polling the gateway's HTTP endpoints, so
# notice delivery is decoupled from gateway availability — a paused cluster
# (migration window) can no longer stall pushes, and the post-pause flood of
# backlogged notices never happens. Row shape mirrors the gateway's
# NoticeItem exactly, so the push/filter logic is unchanged.
_SELECT_NOTICE = (
    "SELECT n.id, n.agent_id, t.label AS agent_label, n.title, n.content, n.priority, "
    "n.require_response, n.question "
    "FROM agent_notices n JOIN agents t ON t.id = n.agent_id "
)


# meiva:note
# One notice as the push and filter logic reads it: the members of the gateway's NoticeItem wire
# shape that a push renders or a filter judges. `question` is the notice's raw question (jsonb).
class Notice(TypedDict):
    id: int
    agent_id: int
    agent_label: NotRequired[str | None]
    title: str
    content: NotRequired[str | None]
    priority: str
    require_response: bool
    question: NotRequired[object]


# meiva:note
# The columns of `_SELECT_NOTICE`.
@dataclass(frozen=True)
class _NoticeRow:
    id: int
    agent_id: int
    agent_label: str | None
    title: str
    content: str | None
    priority: str
    require_response: bool
    question: object


# meiva:note
# The notice push filter: the lowest priority to push and the one agent to push for, each None
# when unset.


# meiva:note
# An armed reply window on one chat: the notice it answers, until when, and the account that
# armed it (None before the account is proven).
class ReplyMode(TypedDict):
    notice_id: int
    agent_id: int
    expires_at: float
    account_id: str | None


_NOTICES = TypeAdapter(list[Notice])
_OBJECT = TypeAdapter(dict[str, object])


def _row_to_notice(r: _NoticeRow) -> Notice:
    """One agent_notices row as the dict the push/filter logic consumes
    (same keys as the gateway's NoticeItem wire shape)."""
    return {
        "id": r.id,
        "agent_id": r.agent_id,
        "agent_label": r.agent_label,
        "title": r.title,
        "content": r.content,
        "priority": r.priority,
        "require_response": r.require_response,
        "question": r.question,
    }


class NoticeBridgeCore(Protocol):
    adapters: dict[str, IMAdapter]
    outbound_store: IMOutboxStore
    gateway: GatewayClient
    _switch_state: dict[str, int]


class NoticeBridge(NoticeBridgeOwner):
    # meiva:note
    # Manual notice lookup and account-bound reply windows for connected IM conversations.

    def __init__(
        self,
        core: NoticeBridgeCore,
        config: ImBridgeConfig,
        *,
        db_pool: ConnectionPool | None = None,
        clock: Clock,
        rng: random.Random,
    ) -> None:
        self._config = config
        self.core: NoticeBridgeCore = core
        self.db_pool: ConnectionPool | None = db_pool
        self._clock = clock
        self._rng = rng
        self._reply_modes: dict[str, ReplyMode] = {}  # chat_id -> mode

    def _now(self) -> float:
        return self._clock.now().timestamp()

    # -- persistence -------------------------------------------------------

    # -- gateway -----------------------------------------------------------

    async def _get(
        self, path: str, params: Mapping[str, str | int | bool] | None = None
    ) -> list[Notice]:
        g = self.core.gateway
        resp = await (await g._http()).get(path, params=params, headers=g._headers())
        resp.raise_for_status()
        return _NOTICES.validate_json(resp.content)

    def _keyed_headers(self) -> dict[str, str]:
        """Auth headers plus one fresh principal-v1 operation key for a resolve call."""
        return {**self.core.gateway._headers(), **keyed_headers(str(new_uuid(self._rng)))}

    async def _post(self, path: str, body: Mapping[str, object]) -> None:
        g = self.core.gateway
        resp = await (await g._http()).post(path, json=body, headers=self._keyed_headers())
        resp.raise_for_status()

    # -- polling -----------------------------------------------------------

    # meiva:note
    # Historical FYI rows receive a filtered receipt without creating an outbound
    # intent. The TTL bounds this compatibility scan; active queues contain only
    # notices that require a response.
    # meiva:link base/events/notifications/docs/notifications.meiva.okf.md

    def _open_notices(self, limit: int | None = None) -> list[Notice]:
        if limit is None:
            limit = self._config.im_bridge_notice_open_limit
        if self.db_pool is None:
            raise RuntimeError("notice bridge requires a database pool")
        with write_transaction(self.db_pool) as conn:
            rows = query_all(
                conn,
                _NoticeRow,
                _SELECT_NOTICE
                + "WHERE n.resolved_at IS NULL AND n.require_response "
                + "ORDER BY n.priority ASC, n.blocking DESC, n.created_at ASC, n.id ASC LIMIT %s",
                (limit,),
            )
        return [_row_to_notice(r) for r in rows]

    # meiva:note
    # Render notices for an explicit queue read or existing account-bound callback.
    # meiva:link base/events/notifications/docs/notifications.meiva.okf.md

    @staticmethod
    def _render_notice(n: Notice, prefix: str | None = None) -> tuple[str, list[tuple[str, str]]]:
        lines = [f"🔔 {n['title']}"]  # emoji-ok: Telegram notification icon (user-facing)
        if prefix:
            lines.append(prefix)
        content = n.get("content")
        if content:
            lines.append(str(content))
        question = n.get("question")
        if question is not None:
            lines.append(question_instructions(parse_question(question)))
        label = n.get("agent_label") or ""
        lines.append(f"[#{n['agent_id']} {label}] · {n['priority']}".rstrip())
        cb = f"{n['agent_id']}:{n['id']}"
        buttons: list[tuple[str, str]] = [
            ("✏️ Reply", f"{_CB_REPLY}{cb}"),  # emoji-ok: button
            ("✖️ Dismiss", f"{_CB_DISMISS}{cb}"),  # emoji-ok: button
        ]
        buttons.append(("📋 Queue", _CB_LIST))  # emoji-ok: queue-view button
        return "\n\n".join(lines), buttons

    #: Cooldown for the "push held" warning — one line per stall, not one per
    #: 3s poll round.
    _push_held_warned_at = 0.0

    # -- callbacks ---------------------------------------------------------

    async def handle_callback(
        self, chat_id: str, data: str, *, account_id: str | None = None
    ) -> str | None:
        """Consume a notice: prefixed button tap; return the hint text to
        show the user (None when the tap is not ours)."""
        if data == _CB_LIST:
            return await self.list_queue()
        if data.startswith(_CB_READ):
            return "Notices require a reply or an explicit dismissal."
        for prefix, action in ((_CB_REPLY, "reply"), (_CB_DISMISS, "dismiss")):
            if data.startswith(prefix):
                agent_id, notice_id = data[len(prefix) :].split(":", 1)
                if action == "reply":
                    return self._arm_reply_mode(chat_id, agent_id, notice_id, account_id=account_id)
                return await self._resolve(action, agent_id, notice_id)
        return None

    def _arm_reply_mode(
        self, chat_id: str, agent_id: str, notice_id: str, *, account_id: str | None = None
    ) -> str:
        """Tap [Reply]: arm the reply window on this chat. A new tap replaces
        an older mode; expired modes are dropped first."""
        window_seconds = self._config.im_bridge_notice_reply_window_seconds
        now = self._now()
        self._reply_modes = {k: v for k, v in self._reply_modes.items() if now <= v["expires_at"]}
        self._reply_modes[str(chat_id)] = {
            "notice_id": int(notice_id),
            "agent_id": int(agent_id),
            "expires_at": now + window_seconds,
            "account_id": account_id,
        }
        return (
            f"✏️ Reply mode: messages you send in the next {window_seconds // 60} min go to"  # emoji-ok: Telegram reply-mode hint (user-facing)
            "that notice as replies (/cancel to exit)"
        )

    async def _resolve(self, action: str, agent_id: str, notice_id: str) -> str:
        # meiva:link services/docs/gateway_side/im_bridge/im_bridge.meiva.okf.md
        await self._post(
            f"/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve",
            {"action": action, "reply": None},
        )
        return "✅ Closed without answering"  # emoji-ok: hint

    async def list_queue(self) -> str | None:
        try:
            if self.db_pool is not None:
                notices = await asyncio.to_thread(self._open_notices)
            else:
                notices = await self._get(
                    "/api/notices/open",
                    {
                        "include_awaiting": True,
                        "limit": self._config.im_bridge_notice_open_limit,
                    },
                )
        except Exception:
            _log.warning("notice list failed", exc_info=True)
            return "Queue lookup failed, try again later"
        if not notices:
            return "🎉 No notices queued"  # emoji-ok: Telegram queue-empty hint
        rendered = [
            self._render_notice(n, prefix=f"Queue {i}/{len(notices)}")[0]
            for i, n in enumerate(notices, start=1)
        ]
        return f"Queue: {len(notices)} notices open\n\n" + "\n\n".join(rendered)

    def cmd_notice(self, args: str) -> str:
        return (
            f"Unsupported notice command {args!r}; use /notice list"
            if args
            else "Use /notice list to read open notices in this conversation"
        )

    def reply_context_held(self, chat_id: str, text: str, account_id: str) -> bool:
        mode = self._reply_modes.get(str(chat_id))
        return bool(
            mode is not None
            and self._now() <= mode["expires_at"]
            and (not text.startswith("/") or text.startswith("/cancel"))
            and mode.get("account_id") != account_id
        )

    def reply_target(self, chat_id: str, text: str, *, account_id: str) -> tuple[int, int] | None:
        """Freeze an account-proven notice target before durable source admission."""
        mode = self._reply_modes.get(str(chat_id))
        if (
            mode is None
            or self._now() > mode["expires_at"]
            or mode.get("account_id") != account_id
            or (text.startswith("/") and not text.startswith("/cancel"))
        ):
            return None
        return mode["agent_id"], mode["notice_id"]

    def clear_reply_target(
        self, chat_id: str, agent_id: int, notice_id: int, *, account_id: str
    ) -> bool:
        mode = self._reply_modes.get(str(chat_id))
        if mode is None or (mode["agent_id"], mode["notice_id"], mode.get("account_id")) != (
            agent_id,
            notice_id,
            account_id,
        ):
            return False
        self._reply_modes.pop(str(chat_id))
        return True

    async def resolve_frozen_reply(
        self, agent_id: int, notice_id: int, text: str
    ) -> dict[str, object]:
        """One call to the original target; return its actual domain response, no fallback."""
        g = self.core.gateway
        response = await (await g._http()).post(
            f"/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve",
            json={"action": "answer", "reply": text},
            headers=self._keyed_headers(),
        )
        response.raise_for_status()
        return _OBJECT.validate_json(response.content)

    async def handle_inbound(self, chat_id: str, text: str) -> str | None:
        """Reply-mode window: plain text resolves the notice; returns a
        confirmation text when consumed, else None (normal flow keeps it)."""
        mode = self._reply_modes.get(str(chat_id))
        if mode is None:
            return None
        if self._now() > mode["expires_at"]:
            self._reply_modes.pop(str(chat_id), None)
            return None
        if text.startswith("/cancel"):
            self._reply_modes.pop(str(chat_id), None)
            return "Left reply mode"
        if text.startswith("/"):
            return None  # other commands pass through to the normal flow
        try:
            await self._post(
                f"/api/keyed/v1/agents/{mode['agent_id']}/notices/{mode['notice_id']}/resolve",
                {"action": "answer", "reply": text},
            )
        except Exception:
            # The notice is probably already resolved (409) or the gateway was
            # briefly unreachable. Pop the mode either way: a stuck reply mode
            # swallows every following user message as another reply attempt
            # and looks like the push stream died (Task #1069 user report).
            _log.warning(
                "notice reply resolve failed notice=%s agent=%s chat=%s — dropping reply mode",
                mode["notice_id"],
                mode["agent_id"],
                chat_id,
                exc_info=True,
            )
            self._reply_modes.pop(str(chat_id), None)
            return copy.REPLY_RESOLVE_FAILED
        self._reply_modes.pop(str(chat_id), None)
        # The reply lands in the notice's agent — if that is not the agent
        # this chat switched to, its follow-up reply will NOT be pushed here
        # (the subscription only covers the switched agent). The reply itself
        # succeeded, so confirm it like any notice reply and add a neutral
        # hint naming the agent the next message will go to (user ruling
        # 2026-08-30).
        switched = self._switched_agent(chat_id)
        if switched is not None and switched != mode["agent_id"]:
            return copy.REPLY_SENT_OTHER_AGENT.format(agent_id=mode["agent_id"], switched=switched)
        return copy.REPLY_SENT

    def _switched_agent(self, chat_id: str) -> int | None:
        """The agent this chat is currently switched to, if any."""

        for key, agent_id in self.core._switch_state.items():
            if key.endswith(f":{chat_id}"):
                return int(agent_id)
        return None

    # -- /notice command ---------------------------------------------------
