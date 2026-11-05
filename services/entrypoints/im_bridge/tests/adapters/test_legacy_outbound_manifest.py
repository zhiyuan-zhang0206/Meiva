# meiva:note
# Retained requests are readable after channel retirement, but only loaded accounts dispatch.
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from services.entrypoints.im_bridge.alert_outbound import AlertAcceptance
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import OutboundIntent
from services.entrypoints.im_bridge.tests.slices import system_rng
from services.entrypoints.im_bridge.tests.test_timeline_outbox import worker_for


@pytest.mark.parametrize("channel", ["telegram", "feishu"])
async def test_retired_channel_requests_remain_readable_without_being_dispatched(
    pool: ConnectionPool, state_dir: Path, channel: str
) -> None:
    request = {
        "channel": channel,
        "chat_id": "42",
        "agent_id": 7,
        "source": {"kind": "message", "identity": "legacy-stored", "block_idx": 0},
        "prepared": {
            "adapter_kind": f"{channel}-v1",
            "account_id": "123",
            "chunks": [{"text": "legacy", "fallback_text": None, "html": False}],
            "markdown": False,
            "buttons": None,
        },
        "replay_id": "",
    }
    intent = OutboundIntent.model_validate(request)
    assert intent.channel == channel
    retained = AlertAcceptance.model_validate(
        {
            "group_id": 1,
            "intent_ids": [1],
            "decisions": [
                {
                    "channel": channel,
                    "account_id": "123",
                    "decision": "available",
                    "recipient": "42",
                    "prepared": request["prepared"],
                }
            ],
        }
    )
    assert retained.decisions[0].channel == channel
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO im_bridge_outbound_intents "
            "(channel,account_id,chat_id,agent_id,source_kind,source_id,block_idx,request) "
            "VALUES (%s,'123','42',7,'message','legacy-stored',0,%s)",
            (channel, Jsonb(request)),
        )
    store = IMOutboxStore(pool, rng=system_rng())
    await worker_for(store, {}).run_once()
    with pool.connection() as conn:
        assert conn.execute("SELECT status FROM im_bridge_outbound_intents").fetchone() == (
            "queued",
        )
