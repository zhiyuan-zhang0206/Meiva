"""Adapter rendering is frozen without tokens before durable acceptance."""

from pathlib import Path

import pytest

from base.clock.tests.fakes import system_clock
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter
from services.entrypoints.im_bridge.tests.slices import system_rng
from services.entrypoints.im_bridge.types import SendNotStartedError


async def test_weixin_preparation_uses_existing_login_identity_not_context_or_token(
    state_dir: Path,
) -> None:
    from services.entrypoints.im_bridge.adapters import weixin

    # meiva:note
    # A test credential for a fake account; the S106 suppression is the harness's.
    weixin.save_account(
        state_dir,
        account_id="bot-id",
        user_id="user",
        bot_token="PRIVATE",  # noqa: S106
        base_url="https://ilink.example",
        saved_at=system_clock().now(),
    )
    adapter = WeixinAdapter(None, state_dir=state_dir, clock=system_clock(), rng=system_rng())
    adapter._tokens.set("chat", "CONTEXT")
    prepared = await adapter.prepare_timeline("hello")
    assert "bot-id" in prepared.account_id
    assert (
        "PRIVATE" not in prepared.model_dump_json() and "CONTEXT" not in prepared.model_dump_json()
    )
    adapter._base_url = "https://user:PRIVATE@ilink.example"
    with pytest.raises(ValueError, match="credentials"):
        await adapter.prepare_timeline("hello")


async def test_native_weixin_owner_uses_login_user_and_holds_missing_owner(
    state_dir: Path,
) -> None:
    from services.entrypoints.im_bridge.adapters import weixin

    # meiva:note
    # A test credential for a fake account; the S106 suppression is the harness's.
    weixin.save_account(
        state_dir,
        account_id="bot",
        user_id="owner",
        bot_token="PRIVATE",  # noqa: S106
        base_url="https://ilink.example",
        saved_at=system_clock().now(),
    )
    adapter = WeixinAdapter(None, state_dir=state_dir, clock=system_clock(), rng=system_rng())
    target, prepared = await adapter.prepare_alert_owner("plain")
    assert target == "owner" and prepared.chunks[0].text == "plain"
    assert "PRIVATE" not in prepared.model_dump_json()
    # meiva:note
    # A test credential for a fake account; the S106 suppression is the harness's.
    weixin.save_account(
        state_dir,
        account_id="bot",
        user_id="",
        bot_token="PRIVATE",  # noqa: S106
        base_url="https://ilink.example",
        saved_at=system_clock().now(),
    )
    ownerless = WeixinAdapter(None, state_dir=state_dir, clock=system_clock(), rng=system_rng())
    with pytest.raises(SendNotStartedError, match="owner"):
        await ownerless.prepare_alert_owner("plain")
