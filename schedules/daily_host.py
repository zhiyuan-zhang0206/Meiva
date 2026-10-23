from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import meiva
from meiva.agents import AgentStatus as S
from schedules.catchup import fire_slot_once
from base.daemon.schedules.watcher import next_fire
from base.db import Database


@dataclass(frozen=True)
class DailyLoopHooks:
    now_fn: Callable[[], datetime] = field(default_factory=lambda: lambda: datetime.now(UTC))
    next_fire_fn: Callable[..., datetime] = field(default_factory=lambda: next_fire)
    claim_fn: Callable[..., bool] = field(default_factory=lambda: fire_slot_once)
    sleep_fn: Callable[[float], None] = field(default_factory=lambda: time.sleep)


def report_agent(
    env_name: str,
    label: str,
    *,
    env: Mapping[str, str] | None = None,
    list_agents_fn: Callable[..., Any] | None = None,
) -> int:
    source_env = os.environ if env is None else env
    list_agents = list_agents_fn or meiva.agents.list_agents
    configured = source_env.get(env_name)
    if configured is not None and configured.strip():
        try:
            agent_id = int(configured)
        except ValueError as exc:
            raise RuntimeError(f"{env_name} must be a numeric agent id") from exc
        if agent_id <= 0:
            raise RuntimeError(f"{env_name} must be a positive agent id")
        return agent_id

    before_id = None
    while True:
        page = list_agents(scope="all", query=label, before_id=before_id)
        for agent in page.agents:
            if agent.label == label and agent.status in (S.RUNNING, S.IDLING, S.TERMINATED):
                return agent.agent_id
        if page.next_cursor is None:
            raise RuntimeError(f"no report agent labelled {label!r} is available")
        before_id = page.next_cursor


def run_daily_loop(
    db: Database,
    cron: str,
    tz: str,
    fire: Callable[[datetime, None], None],
    *,
    hooks: DailyLoopHooks | None = None,
) -> None:
    active = hooks or DailyLoopHooks()
    last_run_at = active.now_fn()
    while True:
        now = active.now_fn()
        next_run = active.next_fire_fn(cron, after=now - timedelta(minutes=2), timezone=tz)
        if next_run <= last_run_at:
            next_run = active.next_fire_fn(cron, after=last_run_at, timezone=tz)
        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds > 0:
            active.sleep_fn(min(wait_seconds, 3600))
            continue
        active.claim_fn(db, next_run, None, fire=fire)
        last_run_at = active.now_fn()
        active.sleep_fn(120)
