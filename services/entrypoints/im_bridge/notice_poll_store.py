"""Normal notice decisions share the existing IM outbound intent transaction."""

from collections.abc import Mapping
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from base.db.rows import query_one
from base.db.transaction import write_transaction
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import (
    NoticePollDecision,
    NoticePollImportReason,
    NoticePollReceipt,
    OutboundIntent,
    OutboundSourceKind,
)


@dataclass(frozen=True)
class _MaximumRow:
    maximum: int


@dataclass(frozen=True)
class _FloorRow:
    legacy_floor: int


@dataclass(frozen=True)
class _AcceptanceRow:
    decision: str
    intent_ids: list[int]


class NoticePollStore:
    """Notice-specific cutover and receipt owner; no separate dispatch queue."""

    def __init__(self, outbound: IMOutboxStore) -> None:
        self.outbound = outbound

    def initialize_notice_poll(self) -> None:
        """Fix the cutover once: notices already committed are never auto-sent."""
        with write_transaction(self.outbound._pool()) as conn:
            maximum = query_one(
                conn, _MaximumRow, "SELECT COALESCE(max(id), 0) AS maximum FROM agent_notices"
            )
            if maximum is None:
                raise RuntimeError("notice cutover aggregate returned no row")
            floor = maximum.maximum
            reason = (
                NoticePollImportReason.NO_HISTORY
                if floor == 0
                else NoticePollImportReason.LEGACY_HISTORY_UNKNOWN
            )
            conn.execute(
                "INSERT INTO im_bridge_notice_poll_state (legacy_floor,import_reason,accepted_notice_id) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (floor, reason.value, floor),
            )

    def accept_notice(
        self,
        notice_id: int,
        snapshot: Mapping[str, object],
        intent: OutboundIntent | None,
        *,
        filtered: bool,
        filtered_reason: str | None = None,
    ) -> NoticePollReceipt:
        """Freeze one normal-poll decision with its shared outbound intent.

        Source receipt lookup comes before mutable target/render comparison:
        an accepted notice cannot be retargeted by a later owner/config change.
        The singleton orders concurrent normal pollers, not provider sends.
        """
        self._validate_notice(notice_id, intent, filtered=filtered)
        with write_transaction(self.outbound._pool()) as conn:
            state = query_one(
                conn,
                _FloorRow,
                "SELECT legacy_floor FROM im_bridge_notice_poll_state WHERE singleton FOR UPDATE",
            )
            if state is None:
                raise RuntimeError("normal notice poll must initialize its cutover first")
            if notice_id <= state.legacy_floor:
                raise ValueError("notice belongs to the retained legacy cutover range")
            previous = query_one(
                conn,
                _AcceptanceRow,
                "SELECT decision,intent_ids FROM im_bridge_notice_acceptances WHERE notice_id=%s",
                (notice_id,),
            )
            if previous is not None:
                return NoticePollReceipt(
                    NoticePollDecision(previous.decision), tuple(previous.intent_ids)
                )
            decision = NoticePollDecision.FILTERED if filtered else NoticePollDecision.QUEUED
            ids = () if filtered else (self.outbound.insert_intent(conn, intent),)
            request = {
                "source": snapshot,
                "intent": intent.model_dump(mode="json") if intent else None,
                "filtered_reason": filtered_reason,
            }
            conn.execute(
                "INSERT INTO im_bridge_notice_acceptances (notice_id,decision,request,intent_ids) "
                "VALUES (%s,%s,%s,%s)",
                (notice_id, decision.value, Jsonb(request), list(ids)),
            )
            conn.execute(
                "UPDATE im_bridge_notice_poll_state SET accepted_notice_id=GREATEST(accepted_notice_id,%s), "
                "updated_at=now() WHERE singleton",
                (notice_id,),
            )
            return NoticePollReceipt(decision, ids)

    @staticmethod
    def _validate_notice(notice_id: int, intent: OutboundIntent | None, *, filtered: bool) -> None:
        if type(notice_id) is not int or notice_id <= 0:
            raise ValueError("normal notice source requires a positive global notice ID")
        if filtered:
            if intent is not None:
                raise ValueError("filtered notices cannot enqueue an intent")
            return
        if intent is None:
            raise ValueError("normal notice acceptance requires its immutable Telegram intent")
        identity = (
            intent.channel,
            intent.source.kind,
            intent.source.identity,
            intent.source.block_idx,
            intent.replay_id,
        )
        if identity != ("telegram", OutboundSourceKind.NOTICE, str(notice_id), 0, ""):
            raise ValueError("normal notice acceptance requires its immutable Telegram intent")
