"""Actual core, registered adapter, native selection and isolated provider transports for the native Weixin tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database, create_agent
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter, save_account
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore
from services.entrypoints.im_bridge.tests.slices import im_bridge_config
from services.entrypoints.im_bridge.tests.support import owned_tasks
from services.entrypoints.im_bridge.types import ChatState


@dataclass
class NativeWeixin:
    core: IMBridgeCore
    adapter: WeixinAdapter
    pool: ConnectionPool
    agent_id: int
    gateway_requests: list[httpx.Request]
    provider_requests: list[httpx.Request]
    provider_responses: list[dict[str, object]]
    provider_send_responses: list[dict[str, object]]
    state_dir: Path


def message(text: str, message_id: object = "1", *, sender: str = "peer-1") -> dict[str, object]:
    return {
        "from_user_id": sender,
        "to_user_id": "bot-id",
        "message_id": message_id,
        "message_type": 1,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


# meiva:note
# The real core with its two background starters (a gateway SSE subscription and the typing
# loop) switched off: the native Weixin tests drive selection and delivery explicitly, and a
# subscription would dial the fixture's gateway transport for traffic the tests never script.
class QuietCore(IMBridgeCore):
    ensured: list[int | None]

    def _ensure_subscription(
        self, state: ChatState, prev_agent: int | None = None, *, catch_up: bool = False
    ) -> None:
        if not hasattr(self, "ensured"):
            self.ensured = []
        self.ensured.append(state.current_agent_id)

    def _start_typing(self, state: ChatState) -> None:
        return None


@pytest.fixture
async def native_weixin(
    database: Database, db_conn: psycopg.Connection, state_dir: Path
) -> AsyncIterator[NativeWeixin]:
    save_account(
        state_dir,
        account_id="bot-id",
        bot_token="isolated-test-token",  # noqa: S106
        user_id="owner",
        base_url="https://provider.example",
    )
    agent_id = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta(id,status) VALUES (%s,'running')", (agent_id,))
    db_conn.commit()
    gateway_requests: list[httpx.Request] = []
    provider_requests: list[httpx.Request] = []
    provider_responses: list[dict[str, object]] = []
    provider_send_responses: list[dict[str, object]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        gateway_requests.append(request)
        if request.method == "GET" and request.url.path == f"/api/agents/{agent_id}":
            return httpx.Response(
                200, json={"agent_id": agent_id, "label": "Selected", "status": "running"}
            )
        if request.method == "GET" and request.url.path.endswith("/timeline"):
            return httpx.Response(200, json={"items": []})
        raise AssertionError("unexpected gateway call in native Weixin fixture")

    def provider(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        if request.url.path.endswith("getupdates"):
            if provider_responses:
                return httpx.Response(200, json=provider_responses.pop(0))
            return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": ""})
        if provider_send_responses:
            return httpx.Response(200, json=provider_send_responses.pop(0))
        return httpx.Response(200, json={"ret": 0})

    config = im_bridge_config(im_send_retry_delays=(0.0,))
    with database.pool(min_size=1, max_size=3) as pool:
        async with (
            owned_tasks() as tasks,
            httpx.AsyncClient(
                base_url="http://isolated-gateway", transport=httpx.MockTransport(gateway)
            ) as gateway_http,
            httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_http,
        ):
            client = GatewayClient(config, gateway_url="http://isolated-gateway", auth_headers={})
            client._client = gateway_http
            core = QuietCore(config, client, db_pool=pool, tasks=tasks, state_dir=state_dir)
            adapter = WeixinAdapter(core, state_dir=state_dir, client=provider_http)
            core.register(adapter)

            store = WeixinIngressStore(pool)
            binding = store.initialize("https://provider.example", "bot-id")
            store.checkpoint(binding, "", "", provider_empty=True)
            state = core._get_or_create_state("weixin", "peer-1")
            await core._cmd_switch(state, str(agent_id), replay_id="fixture-explicit-selection")
            gateway_requests.clear()
            yield NativeWeixin(
                core,
                adapter,
                pool,
                agent_id,
                gateway_requests,
                provider_requests,
                provider_responses,
                provider_send_responses,
                state_dir,
            )
