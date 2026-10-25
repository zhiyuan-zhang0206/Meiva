"""Canonical id-free unified-event bytes shared by every sink."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol


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
    def attributes(self) -> dict[str, Any]: ...


def event_payload(event: _SerializableEvent) -> dict[str, Any]:
    """Return the persistent shape without its derived surrogate id."""
    return {
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


def event_line(event: _SerializableEvent) -> str:
    """Serialize the one byte representation used by JSONL, OTLP, and Loki."""
    return json.dumps(event_payload(event), default=str, separators=(",", ":"), ensure_ascii=False)


def event_line_digest(event: _SerializableEvent) -> str:
    """Return the full byte digest retained by producer receipts."""
    return sha256(event_line(event).encode()).hexdigest()
