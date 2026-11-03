# meiva:note
# The answer of `GET /api/notifications/audit`: the gateway builds it from the audit report and the
# `meiva notices audit` command reads it back, so both name the same fields. Times are the ISO
# strings the audit writes (the OpenAPI schema and the frontend types follow them).
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md

from __future__ import annotations

from pydantic import BaseModel


class AuditWindow(BaseModel):
    start: str
    end: str
    days: int
    timezone: str


class AuditDay(BaseModel):
    day: str
    by_category: dict[str, int]
    total: int


class AuditDelivered(BaseModel):
    total: int
    per_day: list[AuditDay]
    per_category: dict[str, int]
    per_hour: list[int]


class AuditCandidates(BaseModel):
    category: str
    candidates: int
    delivered: int
    suppressed: int
    repeat_deliveries: int


class AuditSimulation(BaseModel):
    variant: str
    delivered: int
    delta: int
    per_day: float


class AuditExplained(BaseModel):
    at: str
    category: str
    agent_id: int
    dedupe_key: str | None
    outcome: str


class NotificationAuditResponse(BaseModel):
    window: AuditWindow
    switches: dict[str, bool]
    categories: list[str]
    sources: dict[str, str]
    delivered: AuditDelivered
    candidates: list[AuditCandidates]
    simulations: list[AuditSimulation]
    explain: list[AuditExplained] | None = None
