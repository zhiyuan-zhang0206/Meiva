# meiva:note
# Provider-account guard readings (health-probe checks 9-10, `meiva cluster health-probe`).
#
# The shell reads what the checks judge: the provider's balance endpoint over HTTP and the
# agent table over the operator database. What the readings mean, and what each check says
# when it fails, is decided in `plans.provider_verdicts`. Everything the checks are
# configured by is read once by the composition root into a `ProviderGuardConfig`.
#
# Both checks ride the health probe's existing machinery on purpose: the 300s OS cron on
# gateway hosts and its `health_probe_failing` event (checks `provider_balance` and
# `provider_blocked_agents`, repeated every run while the condition holds). No new daemon, no
# new schedule, and no dependency on anything the outage kills; in particular not on the LLM
# call chain, which is dead the moment the account is.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeGuard

import httpx
import psycopg

from base.agents.context.clients import DatabaseFactory
from base.agents.recovery.breaker import HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS
from base.config import ConfigBoot
from cli.commands.cluster.plans.provider_verdicts import (
    BalancePayload,
    BalanceUnreadable,
    NoProviderKey,
    ProviderCheck,
    judge_balance,
    judge_blocked_agents,
)


# meiva:note
# What the two checks are configured by. `deepseek_api_key` is None when this host has no
# usable key (unset or blank); the key never reaches a repr.
@dataclass(frozen=True)
class ProviderGuardConfig:
    balance_enabled: bool
    deepseek_api_key: str | None = field(repr=False)
    balance_url: str
    balance_timeout_s: float
    balance_min_cny: float
    blocked_agents_enabled: bool
    blocked_agents_window_hours: float
    blocked_agents_min: int


# meiva:note
# Read the guard's configuration from the command's own configuration owner.
def provider_guard_config(config: ConfigBoot) -> ProviderGuardConfig:
    view = config.view
    guard = view.alerts
    secret = view.lm.deepseek_api_key
    key = None if secret is None else secret.get_secret_value().strip() or None
    return ProviderGuardConfig(
        balance_enabled=guard.provider_guard_balance_enabled,
        deepseek_api_key=key,
        balance_url=guard.provider_guard_balance_url,
        balance_timeout_s=guard.provider_guard_balance_timeout_seconds,
        balance_min_cny=guard.provider_guard_balance_min_cny,
        blocked_agents_enabled=guard.provider_guard_blocked_agents_enabled,
        blocked_agents_window_hours=guard.provider_guard_blocked_agents_window_hours,
        blocked_agents_min=guard.provider_guard_blocked_agents_min,
    )


# meiva:note
# Every enabled check's verdict, balance first (the root cause), blocked agents second. Both
# are observed independently, so a low balance also reports the halted agents it caused. A
# disabled check is not read at all.
def provider_guard_checks(
    guard: ProviderGuardConfig, *, database_factory: DatabaseFactory
) -> tuple[ProviderCheck, ...]:
    checks: list[ProviderCheck] = []
    if guard.balance_enabled:
        checks.append(judge_balance(_read_balance(guard), guard.balance_min_cny))
    if guard.blocked_agents_enabled:
        count = halted_agents_count(
            guard.blocked_agents_window_hours, database_factory=database_factory
        )
        checks.append(
            judge_blocked_agents(count, guard.blocked_agents_min, guard.blocked_agents_window_hours)
        )
    return tuple(checks)


def _read_balance(guard: ProviderGuardConfig) -> NoProviderKey | BalanceUnreadable | BalancePayload:
    if guard.deepseek_api_key is None:
        return NoProviderKey()
    return fetch_balance(guard.balance_url, guard.deepseek_api_key, guard.balance_timeout_s)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# One authenticated read of the provider balance endpoint. Any failure is a value with a
# sanitized reason: an HTTP status, an exception class, or the shape of the answer, never a
# response body or the key.
def fetch_balance(url: str, key: str, timeout_s: float) -> BalancePayload | BalanceUnreadable:
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=timeout_s,
        )
        response.raise_for_status()
        payload: object = response.json()
    except httpx.HTTPStatusError as exc:
        return BalanceUnreadable(f"HTTP {exc.response.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        return BalanceUnreadable(type(exc).__name__)
    if not _is_object_dict(payload):
        return BalanceUnreadable("response is not a JSON object")
    return BalancePayload(payload)


# meiva:note
# Count agents halted by permanent provider rejections inside the window.
#
# `permanent_reject_streak >= HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS` is the recovery
# breaker's durable "halted" flag and the single source of truth for the state (task #3617);
# `last_turn_fatal_at` bounds it to the active wave. A completed turn clears both columns in
# one statement, so the pair stays coherent. None when the query cannot run: the check
# reports "cannot judge" and passes, never a guessed healthy verdict.
def halted_agents_count(window_hours: float, *, database_factory: DatabaseFactory) -> int | None:
    try:
        with database_factory().connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agents_meta"
                " WHERE permanent_reject_streak >= %s"
                "   AND last_turn_fatal_at > now() - make_interval(secs => %s)",
                (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, window_hours * 3600.0),
            )
            row = cur.fetchone()
    except (psycopg.Error, OSError):
        return None
    return int(row[0]) if row is not None else None
