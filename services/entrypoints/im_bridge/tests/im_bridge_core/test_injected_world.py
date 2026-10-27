# meiva:note
# The bridge draws ids, jitter and the time of day from what the daemon hands it. Nothing below
# reads the machine: each test passes the generator or clock it wants and asserts the value that
# came out.

from __future__ import annotations

import pytest
from psycopg_pool import ConnectionPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from base.clock.ids import new_uuid
from base.host.net.tests.local_http import serve_asgi
from services.entrypoints.im_bridge import push_watchdog
from services.entrypoints.im_bridge.adapters.feishu_poll_cursor import anchor, pending_after
from services.entrypoints.im_bridge.adapters.weixin import post_headers, wechat_uin
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore
from services.entrypoints.im_bridge.outbound.push_plan import retry_delay_s
from services.entrypoints.im_bridge.outbound.retry import retry_backoff_seconds
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.tests.slices import im_bridge_config, seeded_rng
from services.entrypoints.im_bridge.tests.test_timeline_outbox import candidate
from services.entrypoints.im_bridge.types import InboundMessage


class _Item:
    def __init__(self, message_id: str, create_time: int) -> None:
        self.message_id = message_id
        self.create_time = str(create_time)


def test_a_minted_id_is_a_version_four_uuid_that_follows_its_generator() -> None:
    first = new_uuid(seeded_rng(9))

    assert first.version == 4
    assert new_uuid(seeded_rng(9)) == first
    assert new_uuid(seeded_rng(10)) != first


def test_retry_backoff_is_the_plan_applied_to_the_injected_generators_draw() -> None:
    config = im_bridge_config(im_push_retry_backoff_seconds=1.0, im_push_retry_jitter_seconds=2.0)

    delay = retry_backoff_seconds(config, rng=seeded_rng(41))

    assert delay == retry_delay_s(base_s=1.0, jitter_s=2.0, draw=seeded_rng(41).random())
    assert retry_backoff_seconds(config, rng=seeded_rng(41)) == delay


def test_wechat_uin_is_the_base64_of_the_drawn_integer() -> None:
    assert wechat_uin(0) == "MA=="
    assert wechat_uin(0xFFFFFFFF) == "NDI5NDk2NzI5NQ=="


@pytest.mark.parametrize("draw", [-1, 0x1_0000_0000])
def test_wechat_uin_refuses_a_draw_outside_a_uint32(draw: int) -> None:
    with pytest.raises(ValueError, match="32 bits"):
        wechat_uin(draw)


def test_request_headers_carry_the_draw_they_were_given() -> None:
    headers = post_headers("secret", uin_draw=7)

    assert headers["X-WECHAT-UIN"] == wechat_uin(7)
    assert headers["Authorization"] == "Bearer secret"


async def test_gateway_send_without_a_key_uses_an_id_drawn_from_the_injected_generator() -> None:
    seen: list[str] = []

    async def accept(request: Request) -> JSONResponse:
        seen.append(request.headers["idempotency-key"])
        return JSONResponse({"status": "delivered"}, status_code=201)

    gateway = Starlette(routes=[Route("/api/agents/405/messages", accept, methods=["POST"])])
    with serve_asgi(gateway) as url:
        client = GatewayClient(
            im_bridge_config(im_send_retry_delays=(0.0,)),
            gateway_url=url,
            auth_headers={},
            rng=seeded_rng(11),
        )
        await client.send_message(405, "hi")

    assert seen == [new_uuid(seeded_rng(11)).hex]


def test_outbound_claim_stamps_the_attempt_with_an_id_from_the_injected_generator(
    pool: ConnectionPool,
) -> None:
    store = IMOutboxStore(pool, rng=seeded_rng(23))
    store.accept("telegram", "bot", "chat", 7, [candidate()])

    claimed = store.claim(("telegram", "bot", "chat"))

    assert claimed is not None
    assert claimed[1] == new_uuid(seeded_rng(23))


def test_a_new_ingress_binding_takes_its_epoch_from_the_injected_generator(
    pool: ConnectionPool,
) -> None:
    binding = WeixinIngressStore(pool, rng=seeded_rng(31)).initialize(
        "https://provider.example", "account"
    )

    assert binding.epoch == new_uuid(seeded_rng(31))


def test_feishu_anchor_of_an_empty_window_is_the_callers_now() -> None:
    assert anchor([], now_ms=1_700_000_000_000) == (None, 1_700_000_000_000)
    newest = _Item("om_2", 1_699_999_000_000)
    assert anchor([_Item("om_1", 1), newest], now_ms=5) == ("om_2", 1_699_999_000_000)


def test_feishu_replay_window_is_measured_from_the_callers_now() -> None:
    old, fresh = _Item("om_1", 1_000_000), _Item("om_2", 1_990_000)

    pending, stale = pending_after(
        [old, fresh], None, None, replay=True, replay_window_s=60.0, now_ms=2_000_000
    )

    assert pending == [fresh]
    assert stale == [old]


class _RecoveredAdapter:
    def __init__(self, recovered_at: float) -> None:
        self.push_recovered_at = recovered_at


class _WatchdogCore:
    def __init__(self, adapter: _RecoveredAdapter) -> None:
        self.config = im_bridge_config()
        self.adapters = {"weixin": adapter}
        self.sent: list[str] = []

    async def _send(
        self, channel: str, chat_id: str, reply: object, *, adapter: object = None
    ) -> None:
        self.sent.append(f"{channel}:{chat_id}")


# meiva:note
# The fake core stands in for the watchdog's structural core protocol, which the type checker cannot
# match against a plain object.
@pytest.mark.parametrize(("now", "hinted"), [(1_030.0, True), (1_061.0, False)])
async def test_the_recovered_hint_is_due_within_its_window_of_the_callers_now(
    now: float, hinted: bool
) -> None:
    core = _WatchdogCore(_RecoveredAdapter(recovered_at=1_000.0))
    message = InboundMessage(channel="weixin", chat_id="peer", text="hi")

    await push_watchdog.hint_recovered(core, message, now=now)  # pyright: ignore[reportArgumentType]

    assert core.sent == (["weixin:peer"] if hinted else [])
