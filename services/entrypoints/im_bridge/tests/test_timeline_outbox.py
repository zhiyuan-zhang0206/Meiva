import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db.transaction import write_transaction
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import (
    OutboundAccountMismatchError,
    OutboundIdentityConflictError,
    OutboundIntent,
    OutboundSource,
    OutboundSourceKind,
    OutboundStatus,
    TimelineCandidate,
)
from services.entrypoints.im_bridge.outbound.worker import IMOutboxWorker
from services.entrypoints.im_bridge.tests.slices import im_bridge_config, seeded_rng
from services.entrypoints.im_bridge.tests.support import failing_insert, failing_update
from services.entrypoints.im_bridge.types import (
    IMAdapter,
    OutboundAdapterKind,
    OutboundChunk,
    PreparedOutboundSend,
    SendNotStartedError,
)


def worker_for(store: IMOutboxStore, adapters: dict[str, IMAdapter]) -> IMOutboxWorker:
    return IMOutboxWorker(store, adapters, config=im_bridge_config(), rng=seeded_rng())


class RecordingAdapter(IMAdapter):
    channel = "telegram"

    def __init__(self, account: str = "bot") -> None:
        super().__init__(None)
        self.account = account
        self.sent: list[str] = []
        self.error: Exception | None = None
        self.error_queue: list[Exception | None] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def start(self, tasks: asyncio.TaskGroup) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def outbound_account_id(self) -> str:
        return self.account

    async def prepare_timeline(self, text: str) -> PreparedOutboundSend:
        return PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.TELEGRAM,
            account_id=self.account,
            chunks=(OutboundChunk(text=text),),
            markdown=False,
        )

    async def send(
        self, chat_id: str, text: str, *, buttons: Any = None, markdown: Any = False
    ) -> None:
        raise AssertionError("timeline delivery must not use command retry owner")

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        self.sent.extend(chunk.text for chunk in prepared.chunks)
        error = self.error_queue.pop(0) if self.error_queue else self.error
        if error is not None:
            raise error


def candidate(
    index: int = 1,
    *,
    text: str = "original",
    account: str = "bot",
    chat: str = "chat",
    agent: int = 7,
    replay: str = "",
    qualified: bool = True,
) -> TimelineCandidate:
    item = {
        "item_id": f"{index}.0",
        "kind": "agent_chat",
        "payload": text,
        "source_message_id": f"persisted-{index}",
        "source_block_idx": 0,
    }
    intent = OutboundIntent(
        channel="telegram",
        chat_id=chat,
        agent_id=agent,
        source=OutboundSource(
            kind=OutboundSourceKind.MESSAGE, identity=f"persisted-{index}", block_idx=0
        ),
        prepared=PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.TELEGRAM,
            account_id=account,
            chunks=(OutboundChunk(text=text),),
            markdown=False,
        ),
        replay_id=replay,
    )
    return TimelineCandidate(item, intent if qualified else None)


def statuses(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT status,attempt_id,outcome_reason FROM im_bridge_outbound_intents ORDER BY id"
        ).fetchall()


def test_acceptance_replays_atomic_batch_and_target_conflicts(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    original = store.accept(
        "telegram", "bot", "chat", 7, [candidate(replay="event")], replay_id="event"
    )
    recovered = store.accept(
        "telegram",
        "bot",
        "chat",
        7,
        [candidate(2, replay="event"), candidate(replay="event")],
        replay_id="event",
    )
    assert recovered == original
    assert len(statuses(pool)) == 1
    with pytest.raises(OutboundIdentityConflictError, match="another target"):
        store.accept(
            "telegram", "bot", "chat", 8, [candidate(agent=8, replay="event")], replay_id="event"
        )


def test_concurrent_acceptance_commits_one_intent_and_cursor(pool: ConnectionPool) -> None:
    def accept(_index: int):
        return IMOutboxStore(pool, new_id=uuid4).accept("telegram", "bot", "chat", 7, [candidate()])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, range(2)))
    assert sum(len(result.intent_ids) for result in results) == 1
    with pool.connection() as conn:
        assert conn.execute("SELECT push_item_id FROM im_bridge_cursors").fetchone() == ("1.0",)
    assert len(statuses(pool)) == 1


def test_failed_insert_rolls_back_cursor_and_earlier_intent(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    with (
        failing_insert(
            db_conn,
            "im_bridge_outbound_intents",
            when="NEW.source_id = 'persisted-2'",
            message="injected insert failure",
        ),
        pytest.raises(psycopg.errors.RaiseException, match="injected"),
    ):
        store.accept("telegram", "bot", "chat", 7, [candidate(), candidate(2)])
    assert statuses(pool) == []
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM im_bridge_cursors").fetchone() == (0,)


def test_changed_manifest_does_not_replace_original(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate(replay="event")], replay_id="event")
    with write_transaction(pool) as conn:
        conn.execute("UPDATE im_bridge_cursors SET push_item_id='0.0'")
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    with write_transaction(pool) as conn:
        conn.execute("UPDATE im_bridge_cursors SET push_item_id='0.0'")
    with pytest.raises(OutboundIdentityConflictError):
        store.accept("telegram", "bot", "chat", 7, [candidate(text="changed")])


def test_legacy_unqualified_blocks_cursor_and_account_rebind_requires_replay(
    pool: ConnectionPool,
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    result = store.accept(
        "telegram", "bot", "chat", 7, [candidate(), candidate(2, qualified=False), candidate(3)]
    )
    assert result.blocked and result.watermark is not None
    assert result.watermark.item_id == "1.0"
    with pytest.raises(OutboundAccountMismatchError):
        store.accept("telegram", "new", "chat", 7, [candidate(2, account="new")])
    store.accept(
        "telegram",
        "new",
        "chat",
        7,
        [candidate(2, account="new", replay="switch")],
        replay_id="switch",
    )
    assert store.pending_streams({"telegram": "new"}) == [("telegram", "new", "chat")]
    assert store.pending_streams({"telegram": "bot"}) == [("telegram", "bot", "chat")]
    store.mark_unavailable_accounts({"telegram": "new"})
    assert statuses(pool)[0][2] == "authenticated_account_unavailable"


def test_legacy_empty_cursor_remains_held_after_account_binding_restart(
    pool: ConnectionPool,
) -> None:
    with write_transaction(pool) as conn:
        conn.execute("INSERT INTO im_bridge_cursors(channel,chat_id) VALUES ('telegram','chat')")
    for _ in range(2):
        result = IMOutboxStore(pool, new_id=uuid4).accept(
            "telegram", "bot", "chat", 7, [candidate()]
        )
        assert result.blocked and not result.intent_ids
    assert statuses(pool) == []
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [], replay_id="empty-switch")
    assert store.accept("telegram", "bot", "chat", 7, [candidate()]).intent_ids


def test_old_accounts_cannot_starve_current_account_limit(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    for index in range(20):
        store.accept(
            "telegram", "old", f"old-{index}", 7, [candidate(account="old", chat=f"old-{index}")]
        )
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    assert store.pending_streams({"telegram": "bot"}) == [("telegram", "bot", "chat")]


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "sent"),
        (SendNotStartedError("no effect"), "failed"),
        (httpx.ReadError("https://provider/botSECRET?context_token=SECRET"), "uncertain"),
    ],
)
async def test_worker_records_whole_send_outcome_without_secret(
    pool: ConnectionPool, error: Any, expected: str
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    adapter.error = error
    await worker_for(store, {"telegram": adapter}).run_once()
    row = statuses(pool)[0]
    assert row[0] == expected and row[1] is not None
    assert "SECRET" not in str(row)
    await worker_for(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original"] * (2 if expected == "failed" else 1)


async def test_worker_retries_a_proved_no_effect_send_once(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    adapter.error_queue = [SendNotStartedError("no effect"), None]
    await worker_for(store, {"telegram": adapter}).run_once()
    row = statuses(pool)[0]
    assert row[0] == "sent"
    assert adapter.sent == ["original", "original"]


async def test_worker_records_failed_when_the_retry_also_proves_no_effect(
    pool: ConnectionPool,
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    adapter.error = SendNotStartedError("no effect")
    await worker_for(store, {"telegram": adapter}).run_once()
    row = statuses(pool)[0]
    assert row[0] == "failed"
    assert adapter.sent == ["original", "original"]
    await worker_for(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original", "original"]


async def test_worker_never_retries_an_ambiguous_outcome(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    adapter.error = httpx.ReadError("https://provider/botSECRET?context_token=SECRET")
    await worker_for(store, {"telegram": adapter}).run_once()
    row = statuses(pool)[0]
    assert row[0] == "uncertain"
    assert adapter.sent == ["original"]


async def test_blocked_send_releases_single_pool_lease_and_other_worker_claims_later(
    pool: ConnectionPool,
) -> None:
    with ConnectionPool[Connection](settings.data_plane.db_url, min_size=1, max_size=1) as small:
        store = IMOutboxStore(small, new_id=uuid4)
        store.accept("telegram", "bot", "chat", 7, [candidate(), candidate(2, text="later")])
        live = RecordingAdapter()
        live.started, live.release = asyncio.Event(), asyncio.Event()
        worker = asyncio.create_task(worker_for(store, {"telegram": live}).run_once())
        try:
            await asyncio.wait_for(live.started.wait(), timeout=5)
            with small.connection(timeout=0.5) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            original_attempt = statuses(pool)[0][1]
            restarted = RecordingAdapter()
            await asyncio.wait_for(
                worker_for(IMOutboxStore(pool, new_id=uuid4), {"telegram": restarted}).run_once(),
                timeout=5,
            )
            assert restarted.sent == ["later"]
            assert statuses(pool)[0] == ("sending", original_attempt, "send_result_unconfirmed")
            worker.cancel()
            await asyncio.sleep(0.02)
            assert not worker.done(), "cancellation still collects the actual SDK send"
        finally:
            live.release.set()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert live.sent == ["original"]
        await worker_for(IMOutboxStore(pool, new_id=uuid4), {"telegram": restarted}).run_once()
        assert [row[0] for row in statuses(pool)] == ["sending", "sent"]
        assert restarted.sent == ["later"]


def test_two_concurrent_claims_cannot_start_the_same_intent(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    [intent_id] = store.accept("telegram", "bot", "chat", 7, [candidate()]).intent_ids

    def claim(_index: int):
        return store.claim(("telegram", "bot", "chat"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1 and claimed[0][0] == intent_id
    assert statuses(pool)[0] == ("sending", claimed[0][1], "send_result_unconfirmed")
    assert not store.finish(intent_id, uuid4(), OutboundStatus.SENT, None)
    assert store.finish(intent_id, claimed[0][1], OutboundStatus.SENT, None)


async def test_claim_without_send_is_unconfirmed_and_does_not_block_later(pool: ConnectionPool):
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate(), candidate(2, text="later")])
    claimed = store.claim(("telegram", "bot", "chat"))
    assert claimed is not None
    adapter = RecordingAdapter()
    await worker_for(store, {"telegram": adapter}).run_once()
    await worker_for(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["later"]
    assert [row[0] for row in statuses(pool)] == ["sending", "sent"]
    assert store.finish(claimed[0], claimed[1], OutboundStatus.SENT, None)


async def test_success_before_outcome_commit_failure_never_repeats_external_send(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    original = store.accept("telegram", "bot", "chat", 7, [candidate()]).intent_ids[0]
    adapter = RecordingAdapter()
    with (
        failing_update(
            db_conn,
            "im_bridge_outbound_intents",
            when="NEW.status = 'sent'",
            message="outcome commit failed",
        ),
        pytest.raises(psycopg.errors.RaiseException, match="outcome commit failed"),
    ):
        await worker_for(store, {"telegram": adapter}).run_once()
    old_attempt = statuses(pool)[0][1]
    await worker_for(store, {"telegram": adapter}).run_once()
    assert statuses(pool)[0][0] == "sending"
    assert adapter.sent == ["original"]
    assert not store.finish(original, uuid4(), OutboundStatus.SENT, None)
    assert statuses(pool)[0][1] == old_attempt
    store.accept("telegram", "bot", "chat", 7, [candidate(2, text="later")])
    await worker_for(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original", "later"]


@pytest.mark.parametrize("finish_fails", [False, True])
async def test_programming_error_is_visible_then_rethrown_without_resend(
    pool: ConnectionPool, db_conn: psycopg.Connection, finish_fails: bool
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    original = TypeError("invalid adapter state SECRET")
    adapter.error = original
    if finish_fails:
        with (
            failing_update(
                db_conn,
                "im_bridge_outbound_intents",
                when="NEW.status = 'uncertain'",
                message="outcome storage failed",
            ),
            pytest.raises(TypeError) as caught,
        ):
            await worker_for(store, {"telegram": adapter}).run_once()
    else:
        with pytest.raises(TypeError) as caught:
            await worker_for(store, {"telegram": adapter}).run_once()
    assert caught.value is original
    if finish_fails:
        assert isinstance(caught.value.__cause__, psycopg.errors.RaiseException)
    else:
        assert caught.value.__cause__ is None
    assert statuses(pool)[0][0] == ("sending" if finish_fails else "uncertain")
    assert "SECRET" not in str(statuses(pool))
    adapter.error = None
    store.accept("telegram", "bot", "chat", 7, [candidate(2, text="later")])
    await worker_for(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original", "later"]


async def test_partial_prefix_is_never_replayed_as_a_whole_manifest(pool: ConnectionPool):
    from services.entrypoints.im_bridge.types import SendOutcomeUncertainError

    class PartialAdapter(RecordingAdapter):
        async def send_prepared_outbound(
            self, chat_id: str, prepared: PreparedOutboundSend
        ) -> None:
            self.sent.append(prepared.chunks[0].text)
            raise SendOutcomeUncertainError("prefix delivered; suffix unknown")

    store = IMOutboxStore(pool, new_id=uuid4)
    first = candidate()
    assert first.intent is not None
    first = TimelineCandidate(
        first.item,
        first.intent.model_copy(
            update={
                "prepared": first.intent.prepared.model_copy(
                    update={"chunks": (OutboundChunk(text="prefix"), OutboundChunk(text="suffix"))}
                )
            }
        ),
    )
    store.accept("telegram", "bot", "chat", 7, [first])
    partial = PartialAdapter()
    await worker_for(store, {"telegram": partial}).run_once()
    await worker_for(store, {"telegram": partial}).run_once()
    assert partial.sent == ["prefix"]
    assert statuses(pool)[0][0] == "uncertain"
    store.accept("telegram", "bot", "chat", 7, [candidate(2, text="later")])
    later = RecordingAdapter()
    await worker_for(store, {"telegram": later}).run_once()
    assert later.sent == ["later"]
    assert [row[0] for row in statuses(pool)] == ["uncertain", "sent"]


async def test_cancelled_send_still_exposes_collected_programming_error(pool: ConnectionPool):
    store = IMOutboxStore(pool, new_id=uuid4)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    started, release = asyncio.Event(), Event()
    original = TypeError("late SDK result invalid")
    loop = asyncio.get_running_loop()

    class SDKAdapter(RecordingAdapter):
        async def send_prepared_outbound(
            self, chat_id: str, prepared: PreparedOutboundSend
        ) -> None:
            def blocking_sdk() -> None:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(timeout=5), "test must release the real SDK thread"
                self.sent.append(prepared.chunks[0].text)
                raise original

            await asyncio.to_thread(blocking_sdk)

    adapter = SDKAdapter()
    task = asyncio.create_task(worker_for(store, {"telegram": adapter}).run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(TypeError) as caught:
        await asyncio.wait_for(task, timeout=5)
    assert caught.value is original
    assert isinstance(original.__cause__, asyncio.CancelledError)
    assert statuses(pool)[0][0] == "uncertain"
    await worker_for(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original"]
