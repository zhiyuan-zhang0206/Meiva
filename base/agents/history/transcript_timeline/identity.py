from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_RECORD_ID = re.compile(r"record:v1:([0-9]{1,19}):([A-Za-z0-9][A-Za-z0-9_.-]{0,255}):[0-9a-f]{64}")


# meiva:link gateway/message_admission/README.md
def record_identity(
    agent_id: int,
    harness: str,
    session: str,
    offset: int,
    entry_key: str | int,
) -> str:
    raw = json.dumps([agent_id, harness, session, offset, entry_key], separators=(",", ":"))
    return f"record:v1:{offset}:{session}:{hashlib.sha256(raw.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RecordQuoteCoordinates:
    offset: int
    session: str


def record_quote_coordinates(identity: str) -> RecordQuoteCoordinates | None:
    match = _RECORD_ID.fullmatch(identity)
    if match is None:
        return None
    offset = int(match[1])
    return RecordQuoteCoordinates(offset, match[2]) if offset < 2**63 else None
