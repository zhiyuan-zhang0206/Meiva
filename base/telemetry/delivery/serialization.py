"""Canonical id-free unified-event bytes shared by every sink, and the surrogate id read from them.

`event_id` hashes the Loki line and its nanosecond timestamp, so the emitter and a Loki reader derive
the same id for one event."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import blake2b, sha256
from typing import Protocol


class _SerializableEvent(Protocol):
    @property
    def ts(self) -> datetime: ...

    @property
    def trace_id(self) -> str | None: ...

    @property
    def span_id(self) -> str | None: ...

    @property
    def agent_id(self) -> int | None: ...

    @property
    def machine(self) -> str: ...

    @property
    def cluster(self) -> str: ...

    @property
    def process(self) -> str: ...

    @property
    def category(self) -> str: ...

    @property
    def event_name(self) -> str: ...

    @property
    def level(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def target_agent_id(self) -> int | None: ...

    @property
    def attributes(self) -> Mapping[str, object]: ...

    @property
    def deployed_sha(self) -> str: ...


def event_payload(event: _SerializableEvent) -> dict[str, object]:
    """Return the persistent shape without its derived surrogate id."""
    payload: dict[str, object] = {
        "ts": event.ts.isoformat(),
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "agent_id": event.agent_id,
        "machine": event.machine,
        "cluster": event.cluster,
        "process": event.process,
        "category": event.category,
        "event_name": event.event_name,
        "level": event.level,
        "source": event.source,
        "target_agent_id": event.target_agent_id,
        "attributes": event.attributes,
    }
    if event.deployed_sha:
        payload["deployed_sha"] = event.deployed_sha
    return payload


def event_line(event: _SerializableEvent) -> str:
    """Serialize the one byte representation used by OTLP and Loki."""
    return json.dumps(event_payload(event), default=str, separators=(",", ":"), ensure_ascii=False)


def event_line_digest(event: _SerializableEvent) -> str:
    """Return the full byte digest retained by producer receipts."""
    return sha256(event_line(event).encode()).hexdigest()


def event_id(line: str, ts_ns: int) -> int:
    return int.from_bytes(blake2b(f"{ts_ns}:{line}".encode(), digest_size=8).digest(), "big")
