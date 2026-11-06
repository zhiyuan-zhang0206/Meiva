# meiva:link agent/graph/exec/docs/process.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from uuid import UUID, uuid5


@dataclass(frozen=True)
class ExecutionIdentity:
    uid: UUID
    issued_at: datetime | None


# meiva:note
# A new sequence over the same execution gives each write in replay order its original UID.
@dataclass(frozen=True)
class OperationIds:
    execution: ExecutionIdentity
    _ordinal: count[int] = field(default_factory=count, init=False, repr=False)

    def next_uid(self) -> UUID:
        return uuid5(self.execution.uid, str(next(self._ordinal)))
