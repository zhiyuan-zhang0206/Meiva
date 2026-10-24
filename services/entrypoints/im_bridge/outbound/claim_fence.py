# meiva:note
# The fence a Weixin command's selection change shares with its source claim.
#
# A command that changes the chat's selection runs in its own transaction, so the transaction
# must prove that the poll binding and the claimed source attempt that admitted the command
# are still current when it writes. `ClaimFence` names that binding and attempt as a value.
# The statements that lock them, and the judgment of their answer, live here as plain data and
# pure functions: the ingress store runs them for its own transactions and the outbound store
# runs them inside the selection transaction.

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

__all__ = [
    "ATTEMPT_SQL",
    "BINDING_SQL",
    "ClaimFence",
    "StalePollBindingError",
    "attempt_parameters",
    "binding_is_current",
]


# meiva:note
# This account generation no longer owns poll admission or checkpoint.
class StalePollBindingError(RuntimeError):
    pass


# meiva:note
# The binding generation and claimed source attempt a transaction must still match.
@dataclass(frozen=True)
class ClaimFence:
    namespace: str
    account_id: str
    epoch: UUID
    receipt_id: int
    attempt_id: UUID | None


# meiva:note
# Lock the singleton binding and its cursor; the row answers `binding_is_current`.
BINDING_SQL = (
    "SELECT b.namespace,b.account_id,b.epoch FROM weixin_ingress_bindings b "
    "JOIN weixin_ingress_cursors c USING(namespace,account_id) WHERE b.id=1 FOR UPDATE OF b,c"
)

# meiva:note
# Lock the claimed source attempt the fence names; a missing row means it was superseded.
ATTEMPT_SQL = (
    "SELECT 1 FROM weixin_ingress_receipts WHERE id=%s AND namespace=%s AND account_id=%s "
    "AND attempt_id=%s AND status='claimed' FOR UPDATE"
)


# meiva:note
# Whether the locked binding row is the generation the caller holds.
def binding_is_current(
    row: tuple[object, ...] | None, *, namespace: str, account_id: str, epoch: UUID
) -> bool:
    return row is not None and tuple(row) == (namespace, account_id, epoch)


# meiva:note
# The parameters of `ATTEMPT_SQL` for one fence.
def attempt_parameters(fence: ClaimFence) -> tuple[int, str, str, UUID | None]:
    return (fence.receipt_id, fence.namespace, fence.account_id, fence.attempt_id)
