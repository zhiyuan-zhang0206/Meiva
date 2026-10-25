# meiva:note
# The provider-account guard's verdicts (health-probe checks 9-10), decided from readings.
#
# Two alert-only checks on the LLM provider account the fleet bills to, added after the
# 2026-09-18 outage: the DeepSeek balance hit zero around 05:02, every deepseek-model call
# answered HTTP 402, 23 agents were halted over 5h43m, and the first signal anyone acted on
# was the owner waking up.
#
# 9. Balance runway: fails while the account balance sits below a configurable minimum, so a
#    top-up lands before the account runs dry. The Grafana billing rule
#    (`meiva-ops-llm-billing-quota`) reports the first post-arrears rejection, already too
#    late; this check is the pre-arrears signal.
# 10. Blocked agents: fails while at least N agents are halted by permanent provider
#    rejections (`agents_meta.permanent_reject_streak >= 2`, the durable recovery-breaker
#    halt in `base/agents/recovery/breaker.py`). The Grafana rule covers billing rejections as
#    events and resolves 15 minutes after the last one; this check covers every permanent
#    class (auth / forbidden / model-not-found included) as a state: it fires while the fleet
#    is still halted and resolves only as agents recover.
#
# Both checks are fail-open on read errors: a provider API blip or an unreachable database is
# not evidence of a drained account, matching the schema check's rule for its own DB flake.
# Live readings (the balance, the halted count) ride a stderr detail line for the cron log;
# the failure messages name the configured threshold.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

__all__ = [
    "BALANCE_CHECK",
    "BLOCKED_AGENTS_CHECK",
    "BalancePayload",
    "BalanceReading",
    "BalanceUnreadable",
    "NoProviderKey",
    "ProviderCheck",
    "judge_balance",
    "judge_blocked_agents",
]

BALANCE_CHECK = "provider_balance"
BLOCKED_AGENTS_CHECK = "provider_blocked_agents"


# meiva:note
# One check's verdict: `failure` is the full `FAIL: ...` line when the check failed (None
# when it passed or could not judge), `notes` are the stderr detail lines it produced, in
# order.
@dataclass(frozen=True)
class ProviderCheck:
    check: str
    failure: str | None
    notes: tuple[str, ...]


# meiva:note
# This host holds no key for the provider, so there is nothing to read the balance with.
@dataclass(frozen=True)
class NoProviderKey: ...


# meiva:note
# The balance endpoint did not produce a payload; `reason` is sanitized (an HTTP status or an
# exception class, never a response body or a key).
@dataclass(frozen=True)
class BalanceUnreadable:
    reason: str


# meiva:note
# The decoded JSON object the balance endpoint answered.
@dataclass(frozen=True)
class BalancePayload:
    body: Mapping[str, object]


BalanceReading = NoProviderKey | BalanceUnreadable | BalancePayload


def _skipped(why: str) -> ProviderCheck:
    return ProviderCheck(BALANCE_CHECK, None, (f"  (provider balance check skipped: {why})",))


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The CNY `total_balance` from a DeepSeek /user/balance payload, or None. The guard compares
# in CNY, the account's funded currency, only; a payload without a CNY entry cannot be judged
# against a CNY minimum and passes with a stderr note rather than guessing at FX. A payload
# that is not shaped like a balance answer is a read error, not a verdict.
def _cny_balance(payload: Mapping[str, object]) -> float | None:
    infos = payload.get("balance_infos")
    if not _is_object_list(infos):
        raise TypeError("response has no balance_infos")
    for entry in infos:
        if not _is_object_dict(entry):
            continue
        if entry.get("currency") != "CNY":
            continue
        raw_balance = entry.get("total_balance")
        if not isinstance(raw_balance, str | int | float) or isinstance(raw_balance, bool):
            raise ValueError("CNY total_balance is not numeric")  # noqa: TRY004
        try:
            return float(raw_balance)
        except ValueError:
            raise ValueError("CNY total_balance is not numeric") from None
    return None


# meiva:note
# Check 9: no failure when the key is absent, the balance is unreadable, or it is funded
# enough. An account the provider reports unavailable fails even when funded.
def judge_balance(reading: BalanceReading, minimum_cny: float) -> ProviderCheck:
    if isinstance(reading, NoProviderKey):
        return _skipped("DEEPSEEK_API_KEY not configured on this host")
    if isinstance(reading, BalanceUnreadable):
        return _skipped(reading.reason)
    try:
        total_cny = _cny_balance(reading.body)
    except (TypeError, ValueError) as exc:
        return _skipped(str(exc))
    if total_cny is None:
        return _skipped("no CNY balance entry in the response")
    available = reading.body.get("is_available") is not False
    if available and total_cny >= minimum_cny:
        return ProviderCheck(BALANCE_CHECK, None, ())
    detail = (
        f"  (provider balance detail: ¥{total_cny:.2f}, available={available}, "
        f"minimum=¥{minimum_cny:g})"
    )
    if not available:
        cause = (
            "the provider reports the account as unavailable (is_available=false); "
            "check the account's billing state"
        )
    else:
        cause = (
            "account balance below the configured minimum "
            f"(MEIVA_PROVIDER_GUARD_BALANCE_MIN_CNY=¥{minimum_cny:g}); top up the "
            "provider account before it runs dry"
        )
    return ProviderCheck(BALANCE_CHECK, f"FAIL: provider balance — {cause}", (detail,))


# meiva:note
# Check 10: no failure when the halted count is unreadable (the agent table could not be
# queried: "cannot judge" passes, never a guessed healthy verdict) or under the threshold.
def judge_blocked_agents(count: int | None, minimum: int, window_hours: float) -> ProviderCheck:
    if count is None:
        note = "  (provider blocked-agents check skipped: agent table unreachable)"
        return ProviderCheck(BLOCKED_AGENTS_CHECK, None, (note,))
    if count < minimum:
        return ProviderCheck(BLOCKED_AGENTS_CHECK, None, ())
    detail = (
        f"  (provider blocked-agents detail: {count} agent(s) halted by permanent "
        f"provider rejections within {window_hours:g}h)"
    )
    cause = (
        "multiple agents are halted by permanent provider rejections "
        f"(threshold MEIVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN={minimum}); automatic recovery "
        "stays off until a turn succeeds — resolve the provider cause, then revive the "
        "affected agents"
    )
    return ProviderCheck(
        BLOCKED_AGENTS_CHECK, f"FAIL: provider blocked agents — {cause}", (detail,)
    )
