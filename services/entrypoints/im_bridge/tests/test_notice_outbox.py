"""Normal notice acceptance uses real receipts, cutover and shared dispatch."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import NoticePollImportReason
from services.entrypoints.im_bridge.tests.slices import im_bridge_config
from services.entrypoints.im_bridge.tests.test_im_bridge_core import FakeGateway
from services.entrypoints.im_bridge.tests.test_timeline_outbox import RecordingAdapter
from services.entrypoints.im_bridge.tests.test_timeline_outbox import pool as pool
from services.entrypoints.im_bridge.types import (
    OutboundAdapterKind,
    OutboundChunk,
    PreparedOutboundSend,
)


class NoticeAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__("bot")
        self.recipient: str | None = "owner-a"
        self.deliveries: list[tuple[str, str, tuple[tuple[str, str], ...] | None]] = []

    async def prepare_notice_owner(
        self, text: str, buttons: tuple[tuple[str, str], ...]
    ) -> tuple[str, PreparedOutboundSend]:
        if self.recipient is None:
            raise NotImplementedError("notice owner unavailable")
        return self.recipient, PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.TELEGRAM,
            account_id=self.account,
            chunks=(OutboundChunk(text=text),),
            markdown=False,
            buttons=buttons,
        )

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        self.deliveries.append((chat_id, prepared.chunks[0].text, prepared.buttons))
        await super().send_prepared_outbound(chat_id, prepared)


def make_core(pool: ConnectionPool, **config: Any) -> tuple[IMBridgeCore, NoticeAdapter]:
    core = IMBridgeCore(
        im_bridge_config(**config),
        FakeGateway(),  # type: ignore[arg-type]
        db_pool=pool,
        tasks=asyncio.TaskGroup(),
    )
    adapter = NoticeAdapter()
    core.register(adapter)
    return core, adapter


def insert_notice(
    conn: Connection,
    agent: int,
    local: int,
    title: str = "notice",
    *,
    require_response: bool = True,
) -> int:
    row = conn.execute(
        "INSERT INTO agent_notices (agent_id,local_id,title,content,priority,require_response,blocking,expire_at) "
        "VALUES (%s,%s,%s,'body','P2',%s,false,now()+interval '1 day') RETURNING id",
        (agent, local, title, require_response),
    ).fetchone()
    assert row is not None
    return int(row[0])


def receipts(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT notice_id,decision,request,intent_ids FROM im_bridge_notice_acceptances ORDER BY notice_id"
        ).fetchall()


async def test_new_install_accepts_atomically_and_worker_sends_frozen_target_buttons(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.initialize_poll()  # The daemon initializes before readiness, not a user command.
    with pool.connection() as conn:
        notice = insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
        )
    await core.notice_bridge.poll_once()
    accepted = receipts(pool)
    assert accepted[0][0] == notice and accepted[0][1] == "queued"
    assert adapter.deliveries == [], "normal poll must not contact the provider"
    frozen = accepted[0][2]["intent"]
    assert frozen["chat_id"] == "owner-a" and frozen["prepared"]["account_id"] == "bot"
    assert frozen["prepared"]["buttons"][0][1].endswith(f":{notice}")
    adapter.recipient = "owner-b"
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 1 and adapter.deliveries[0][0] == "owner-a"
    assert adapter.deliveries[0][2] == tuple(
        tuple(button) for button in frozen["prepared"]["buttons"]
    )
    assert len(receipts(pool)) == 1


async def test_two_connections_reverse_commit_do_not_skip_late_lower_id(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll()
    agent = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with pool.connection() as low:
        low_id = insert_notice(low, agent, 0, "low")
        with pool.connection() as high:
            high_id = insert_notice(high, agent, 1, "high")
        assert high_id > low_id
        await core.notice_bridge.poll_once()
        assert [r[0] for r in receipts(pool)] == [high_id]
    await core.notice_bridge.poll_once()
    assert [r[0] for r in receipts(pool)] == [low_id, high_id]
    for _ in range(2):
        await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 2


def cutover(pool: ConnectionPool) -> tuple[int, str]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT legacy_floor,import_reason FROM im_bridge_notice_poll_state"
        ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


async def test_first_initialization_retains_existing_history_only(
    redis_url: str,
    pool: ConnectionPool,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    with pool.connection() as conn:
        old = insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
            "old history",
        )
    # A cursor file left by an earlier generation is neither read nor rewritten.
    path = tmp_path / "state" / "im_bridge" / "notice_cursor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("0")
    core, adapter = make_core(pool)
    await core.notice_bridge.poll_once()
    assert not receipts(pool) and not adapter.deliveries
    assert cutover(pool) == (old, NoticePollImportReason.LEGACY_HISTORY_UNKNOWN.value)
    with pool.connection() as conn:
        new = insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
            "new range",
        )
    await core.notice_bridge.poll_once()
    assert [r[0] for r in receipts(pool)] == [new]
    await core.outbound_worker.run_once()
    assert "new range" in adapter.deliveries[0][1]
    assert cutover(pool) == (old, NoticePollImportReason.LEGACY_HISTORY_UNKNOWN.value)
    assert path.read_text() == "0"
    # Explicit listing remains available; it is intentionally a different producer.
    assert any(n["id"] == old for n in core.notice_bridge._open_notices())


async def test_empty_history_initializes_at_floor_zero(pool: ConnectionPool) -> None:
    core, _ = make_core(pool)
    core.notice_bridge.initialize_poll()
    assert cutover(pool) == (0, NoticePollImportReason.NO_HISTORY.value)


async def test_filtered_decision_commits_without_owner_and_does_not_later_replay(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool, im_bridge_notice_push_scope="all")
    core.notice_bridge.poll_store.initialize_notice_poll()
    with pool.connection() as conn:
        notice = insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
            require_response=False,
        )
    adapter.recipient = None
    core.notice_bridge._filters = {"min_priority": "P1", "agent": None}
    await core.notice_bridge.poll_once()
    assert receipts(pool)[0][0:2] == (notice, "filtered")
    assert receipts(pool)[0][3] == []
    core.notice_bridge._filters = {"min_priority": None, "agent": None}
    adapter.recipient = "owner-a"
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert not adapter.deliveries


async def test_no_owner_holds_without_fake_receipt_then_accepts_when_available(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll()
    with pool.connection() as conn:
        insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
        )
    adapter.recipient = None
    await core.notice_bridge.poll_once()
    assert not receipts(pool)
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone() == (0,)
    adapter.recipient = "owner-a"
    await core.notice_bridge.poll_once()
    assert len(receipts(pool)) == 1


async def test_acceptance_rollback_then_lost_response_converge_without_inline_send(
    redis_url: str,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll()
    with pool.connection() as conn:
        insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
        )
    insert = IMOutboxStore.insert_intent

    def rollback(conn: Connection, intent: Any) -> int:
        insert(conn, intent)
        raise RuntimeError("crash after insert before receipt commit")

    monkeypatch.setattr(IMOutboxStore, "insert_intent", staticmethod(rollback))
    with pytest.raises(RuntimeError, match="crash after insert"):
        await core.notice_bridge.poll_once()
    assert not receipts(pool)
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone() == (0,)
        assert conn.execute(
            "SELECT accepted_notice_id FROM im_bridge_notice_poll_state"
        ).fetchone() == (0,)
    monkeypatch.setattr(IMOutboxStore, "insert_intent", staticmethod(insert))
    accept = core.notice_bridge.poll_store.accept_notice

    def lost(*args: Any, **kwargs: Any) -> Any:
        accept(*args, **kwargs)
        raise RuntimeError("response lost after durable acceptance")

    monkeypatch.setattr(core.notice_bridge.poll_store, "accept_notice", lost)
    with pytest.raises(RuntimeError, match="response lost"):
        await core.notice_bridge.poll_once()
    assert len(receipts(pool)) == 1
    monkeypatch.setattr(core.notice_bridge.poll_store, "accept_notice", accept)
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 1


async def test_concurrent_normal_acceptances_freeze_first_target_without_duplicate(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.poll_store.initialize_notice_poll()
    with pool.connection() as conn:
        notice = insert_notice(
            conn,
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            ),
            0,
        )
    snapshot = core.notice_bridge._unaccepted_notices()[0]

    def accept(_index: int) -> None:
        asyncio.run(core.notice_bridge._accept_normal_notice(snapshot, switches={}))

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(accept, range(2)))
    assert len(receipts(pool)) == 1 and len(receipts(pool)[0][3]) == 1
    adapter.error = httpx.ReadError("https://api.telegram.org/botSECRET/sendMessage")
    await core.outbound_worker.run_once()
    adapter.error = None
    await core.notice_bridge.poll_once()
    await core.outbound_worker.run_once()
    assert len(adapter.deliveries) == 1
    with pool.connection() as conn:
        status = conn.execute(
            "SELECT status,outcome_reason FROM im_bridge_outbound_intents"
        ).fetchone()
        assert status is not None and status[0] == "uncertain" and "SECRET" not in str(status)
    assert receipts(pool)[0][0] == notice


# meiva:note
# Which notices reach the chat is one decision with a recorded reason. The default
# holds an FYI back (`filtered_reason='fyi'`); `all` is the configuration rollback;
# a switched-off `core.needs_input` silences decisions on this transport too.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
@pytest.mark.parametrize(("scope", "fyi_pushed"), [("decisions", False), ("all", True)])
async def test_push_scope_decides_whether_an_fyi_reaches_the_chat(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    scope: str,
    fyi_pushed: bool,
) -> None:
    core, adapter = make_core(pool, im_bridge_notice_push_scope=scope)
    core.notice_bridge.initialize_poll()
    agent = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with pool.connection() as conn:
        fyi = insert_notice(conn, agent, 0, "internal progress note", require_response=False)
        decision = insert_notice(conn, agent, 1, "ship it?")
    await core.notice_bridge.poll_once()
    for _ in range(2):
        await core.outbound_worker.run_once()

    sent = [text for _chat, text, _buttons in adapter.deliveries]
    assert len(sent) == (2 if fyi_pushed else 1)
    assert any("ship it?" in text for text in sent)
    assert ("internal progress note" in " ".join(sent)) is fyi_pushed
    by_notice = {row[0]: row for row in receipts(pool)}
    assert by_notice[fyi][1] == ("queued" if fyi_pushed else "filtered")
    assert by_notice[fyi][2]["filtered_reason"] == (None if fyi_pushed else "fyi")
    assert by_notice[decision][1] == "queued"


# meiva:note
# The switch belongs to the "needs an answer" category, not to the notice kind: an
# FYI keeps its own reason when that switch is off, and `all` still restores it.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
@pytest.mark.parametrize(("scope", "fyi_pushed"), [("decisions", False), ("all", True)])
async def test_a_switched_off_category_does_not_reclassify_an_fyi(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    scope: str,
    fyi_pushed: bool,
) -> None:
    core, adapter = make_core(pool, im_bridge_notice_push_scope=scope)
    core.notice_bridge.initialize_poll()
    agent = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with pool.connection() as conn:
        fyi = insert_notice(conn, agent, 0, "internal progress note", require_response=False)
        conn.execute(
            "INSERT INTO user_settings (key, value) VALUES ('notifications.categories', "
            "jsonb_build_object('core.needs_input', false)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    try:
        await core.notice_bridge.poll_once()
        await core.outbound_worker.run_once()

        row = receipts(pool)[0]
        assert row[0] == fyi
        assert row[1] == ("queued" if fyi_pushed else "filtered")
        assert row[2]["filtered_reason"] == (None if fyi_pushed else "fyi")
        assert len(adapter.deliveries) == (1 if fyi_pushed else 0)
    finally:
        with pool.connection() as conn:
            conn.execute("DELETE FROM user_settings WHERE key = 'notifications.categories'")


async def test_a_switched_off_needs_input_category_silences_decisions(
    redis_url: str,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    core, adapter = make_core(pool)
    core.notice_bridge.initialize_poll()
    agent = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with pool.connection() as conn:
        decision = insert_notice(conn, agent, 0, "ship it?")
        conn.execute(
            "INSERT INTO user_settings (key, value) VALUES ('notifications.categories', "
            "jsonb_build_object('core.needs_input', false)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    try:
        await core.notice_bridge.poll_once()
        await core.outbound_worker.run_once()

        assert adapter.deliveries == []
        row = receipts(pool)[0]
        assert row[0] == decision and row[1] == "filtered"
        assert row[2]["filtered_reason"] == "needs_input_off"
    finally:
        with pool.connection() as conn:
            conn.execute("DELETE FROM user_settings WHERE key = 'notifications.categories'")
