"""Pre-send failures authorize a full-message retry only before the first acknowledgement."""

from pathlib import Path

import httpx
import pytest

from base.clock.tests.fakes import system_clock
from services.entrypoints.im_bridge.adapters import telegram, weixin
from services.entrypoints.im_bridge.tests.slices import system_rng, telegram_config
from services.entrypoints.im_bridge.types import SendNotStartedError


@pytest.mark.parametrize("channel", ["telegram", "weixin"])
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_connect_failure_marker_cannot_escape_after_a_successful_prefix(
    channel: str,
    acknowledged: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_dir: Path,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if acknowledged and len(calls) == 1:
            return httpx.Response(200, json={"ok": True, "result": {}, "ret": 0})
        raise httpx.ConnectError("not connected", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if channel == "telegram":
            adapter = telegram.TelegramAdapter(
                None,
                telegram_config(
                    telegram_bot_token="test-token",  # noqa: S106 - non-secret test fixture
                    telegram_owner_id=42,
                ),
                client=client,
                state_dir=state_dir,
            )
            text = "a" * 5000
        else:
            # meiva:note
            # A test credential for a fake account; the S106 suppression is the harness's.
            weixin.save_account(
                state_dir,
                account_id="test",
                base_url="https://ilink.example",
                bot_token="test",  # noqa: S106
                user_id="owner",
                saved_at=system_clock().now(),
            )
            adapter = weixin.WeixinAdapter(
                None,
                client=client,
                state_dir=state_dir,
                clock=system_clock(),
                rng=system_rng(),
            )
            adapter._chunk_delay_seconds = 0
            text = "a" * 3000
        if acknowledged:
            with pytest.raises(RuntimeError, match="acknowledged chunks") as error:
                await adapter.send("chat", text)
            assert not isinstance(error.value, SendNotStartedError)
        else:
            with pytest.raises(SendNotStartedError):
                await adapter.send("chat", text)
    assert len(calls) == (2 if acknowledged else 1)
