"""Boot owner-seed regression tests for the Feishu adapter (task #4930).

The owner open id is memory-only, and the daemon no longer replays user
messages across a restart, so the outbound leg went blind until the user's next
message (measured 6h+; only the daily patrol noticed). ``start()`` now seeds the
owner from the persisted switch state when exactly one feishu chat is recorded.
These tests pin the seed semantics: first-writer, no guessing on a missing /
unreadable / ambiguous state, and the ``im_feishu_owner_seed_failed`` event
that keeps the blind window observable instead of silent.

``send_to_owner`` plumbing is covered by ``test_feishu_adapter.py``; the send
seam used here only proves the seed resolves the owner.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from services.entrypoints.im_bridge.adapters.feishu import FeishuAdapter
from services.entrypoints.im_bridge.config import FeishuCredentialsConfig
from services.entrypoints.im_bridge.tests.adapters.test_feishu_adapter import (
    FakeRestClient,
    FakeWsClient,
    websocket_ready,
)
from services.entrypoints.im_bridge.tests.slices import feishu_config
from services.entrypoints.im_bridge.tests.support import owned_tasks
from services.entrypoints.im_bridge.types import InboundMessage


class FakeCore:
    """Adapter-side core double: inbound sink + the adapters registry."""

    def __init__(self) -> None:
        self.received: list[InboundMessage] = []
        self.adapters: dict[str, Any] = {}

    async def handle_inbound(self, message: InboundMessage) -> None:
        self.received.append(message)


def _write_switch_state(state_dir: Path, state: dict[str, int]) -> None:
    path = state_dir / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _seed_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r["extra"] for r in records if r["extra"].get("event") == "im_feishu_owner_seed_failed"]


def _boot_adapter(
    config: FeishuCredentialsConfig,
    state_dir: Path,
    core: FakeCore | None = None,
    ws_client: FakeWsClient | None = None,
) -> FeishuAdapter:
    return FeishuAdapter(
        core or FakeCore(),
        config,
        state_dir=state_dir,
        proxy_configured=False,
        ws_client=ws_client,
    )


# -- boot path (start) ------------------------------------------------------


async def test_start_seeds_owner_open_id_from_switch_state(tmp_path: Path, state_dir: Path) -> None:
    """The restart regression: with exactly one feishu chat recorded, boot
    restores the owner open id before the link goes live."""
    async with owned_tasks() as _owned_tasks:
        _write_switch_state(state_dir, {"feishu:ou_owner_1": 405, "telegram:12345": 405})
        core = FakeCore()
        ws_client = FakeWsClient()
        adapter = _boot_adapter(
            feishu_config(
                feishu_app_id="cli_x",
                feishu_app_secret="secret_x",  # noqa: S106
                feishu_poll_interval_seconds=0,
            ),
            state_dir,
            core,
            ws_client,
        )

        await asyncio.wait_for(adapter.start(_owned_tasks), timeout=5)
        adapter.begin_shutdown()
        ws_client.disconnected.set()

        assert adapter._last_open_id == "ou_owner_1"


async def test_start_without_credentials_does_not_seed_or_alert(
    tmp_path: Path, state_dir: Path
) -> None:
    """A disabled feishu link (no credentials) has no outbound leg to rescue:
    no seed, no alert — the alert exists for a live-but-blind leg."""
    async with owned_tasks() as _owned_tasks:
        _write_switch_state(state_dir, {"feishu:ou_owner_1": 405})
        core = FakeCore()
        adapter = _boot_adapter(
            feishu_config(feishu_app_id="", feishu_app_secret=""), state_dir, core
        )

        await adapter.start(_owned_tasks)

        assert adapter._last_open_id == ""


# -- seed semantics ---------------------------------------------------------


# meiva:note
# Positive case: after the seed, send_to_owner resolves the owner instead of raising
# 'no known user chat' (send call stubbed at the seam).
async def test_seed_lets_send_to_owner_reach_the_seeded_owner(state_dir: Path) -> None:
    _write_switch_state(state_dir, {"feishu:ou_owner_1": 405})
    rest = FakeRestClient()
    adapter = FeishuAdapter(
        FakeCore(),
        feishu_config(),
        state_dir=state_dir,
        proxy_configured=False,
        rest_client=rest,
    )
    adapter._app_id = "cli_x"
    adapter._app_secret = "secret_x"  # noqa: S105
    await adapter._seed_owner_from_switch_state()

    async with websocket_ready(adapter):
        await adapter.send_to_owner("hi")

    assert len(rest.created) == 1
    assert rest.created[0].request_body.receive_id == "ou_owner_1"
    assert json.loads(rest.created[0].request_body.content)["text"] == "hi"


async def test_seed_never_overwrites_a_known_owner(state_dir: Path) -> None:
    """First-writer: an owner already known (e.g. from an inbound message) is
    never replaced by the persisted state — the rule _register_sent_chat keeps."""
    _write_switch_state(state_dir, {"feishu:ou_state": 405})
    adapter = FeishuAdapter(
        FakeCore(), feishu_config(), state_dir=state_dir, proxy_configured=False
    )
    adapter._last_open_id = "ou_known"

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == "ou_known"


@pytest.mark.parametrize(
    "state",
    [None, {}, {"telegram:12345": 405}],
    ids=["missing-file", "empty-state", "no-feishu-key"],
)
async def test_seed_without_a_source_emits_the_failure_event_and_stays_blind(
    state: dict[str, int] | None, loguru_records: list[dict[str, Any]], state_dir: Path
) -> None:
    """No usable source (state missing / empty / no feishu chat) -> stay empty
    and emit the failure event, so the blind leg is observable."""
    if state is not None:
        _write_switch_state(state_dir, state)
    adapter = FeishuAdapter(
        FakeCore(), feishu_config(), state_dir=state_dir, proxy_configured=False
    )

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == ""
    events = _seed_events(loguru_records)
    assert [(e["reason"], e["chats"]) for e in events] == [("no_source", 0)]


async def test_seed_with_multiple_feishu_chats_emits_the_ambiguity_event(
    loguru_records: list[dict[str, Any]], state_dir: Path
) -> None:
    """Two recorded feishu chats are ambiguous: never guess one — emit instead."""
    _write_switch_state(state_dir, {"feishu:ou_a": 405, "feishu:ou_b": 405})
    adapter = FeishuAdapter(
        FakeCore(), feishu_config(), state_dir=state_dir, proxy_configured=False
    )

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == ""
    events = _seed_events(loguru_records)
    assert [(e["reason"], e["chats"]) for e in events] == [("ambiguous", 2)]


async def test_seed_with_invalid_state_fails_without_blind_fallback(
    loguru_records: list[dict[str, Any]], state_dir: Path
) -> None:
    """An existing invalid switch-state image fails startup."""
    path = state_dir / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    adapter = FeishuAdapter(
        FakeCore(), feishu_config(), state_dir=state_dir, proxy_configured=False
    )

    with pytest.raises(ValueError):
        await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == ""
    assert _seed_events(loguru_records) == []
