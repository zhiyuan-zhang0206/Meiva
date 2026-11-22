from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from base.agents.model.harness_models import resolve_harness_catalog_model
from base.lm.pricing import CacheTokens, PriceBook, quote


@dataclass(frozen=True)
class UnpricedBackfillModelSummary:
    raw_model: str
    canonical_model: str
    observations_priced: int
    observations_still_unpriced: int
    added_cost_usd: Decimal


@dataclass(frozen=True)
class UnpricedBackfillReport:
    dry_run: bool
    observations_scanned: int
    observations_priced: int
    observations_still_unpriced: int
    metric_days_recomputed: int
    ledger_days_scanned: int
    ledger_days_updated: int
    ledger_calls_priced: int
    totals_rebuilt: bool
    added_observation_cost_usd: Decimal
    added_observation_cost_usd_subscription: Decimal
    by_model: tuple[UnpricedBackfillModelSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "observations_scanned": self.observations_scanned,
            "observations_priced": self.observations_priced,
            "observations_still_unpriced": self.observations_still_unpriced,
            "metric_days_recomputed": self.metric_days_recomputed,
            "ledger_days_scanned": self.ledger_days_scanned,
            "ledger_days_updated": self.ledger_days_updated,
            "ledger_calls_priced": self.ledger_calls_priced,
            "totals_rebuilt": self.totals_rebuilt,
            "added_observation_cost_usd": float(self.added_observation_cost_usd),
            "added_observation_cost_usd_subscription": float(
                self.added_observation_cost_usd_subscription
            ),
            "by_model": [
                {
                    "raw_model": item.raw_model,
                    "canonical_model": item.canonical_model,
                    "observations_priced": item.observations_priced,
                    "observations_still_unpriced": item.observations_still_unpriced,
                    "added_cost_usd": float(item.added_cost_usd),
                }
                for item in self.by_model
            ],
        }


@dataclass(frozen=True)
class UnpricedObsRow:
    event_id: Decimal
    agent_id: int
    occurred_at: datetime
    model: str | None
    usage_calls: int
    unpriced_calls: int
    unpriced_calls_subscription: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int


@dataclass(frozen=True)
class PricedLedgerRow:
    agent_id: int
    model: str
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float
    cost_usd_subscription: float


@dataclass(frozen=True)
class UnpricedTelemetryRow:
    agent_id: int
    ts: datetime
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_30m: int
    tokens_reasoning: int
    is_subscription: bool


@dataclass(frozen=True)
class ExistingLedgerRow:
    agent_id: int
    model: str
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float
    cost_usd_subscription: float
    costed_calls: int
    unpriced_calls: int
    unpriced_calls_subscription: int
    estimated_calls: int


@dataclass(frozen=True)
class _ModelStat:
    priced: int = 0
    still_unpriced: int = 0
    added_cost_usd: Decimal = Decimal(0)


@dataclass(frozen=True)
class LedgerBucket:
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_reasoning: int = 0
    cost_usd: float = 0.0
    cost_usd_subscription: float = 0.0
    costed_calls: int = 0
    unpriced_calls: int = 0
    unpriced_calls_subscription: int = 0
    calls_newly_priced: int = 0

    def add_priced(self, row: PricedLedgerRow) -> LedgerBucket:
        return LedgerBucket(
            llm_calls=self.llm_calls + row.llm_calls,
            tokens_in=self.tokens_in + row.tokens_in,
            tokens_out=self.tokens_out + row.tokens_out,
            tokens_cached=self.tokens_cached + row.tokens_cached,
            tokens_reasoning=self.tokens_reasoning + row.tokens_reasoning,
            cost_usd=self.cost_usd + row.cost_usd,
            cost_usd_subscription=self.cost_usd_subscription + row.cost_usd_subscription,
            costed_calls=self.costed_calls + row.llm_calls,
            unpriced_calls=self.unpriced_calls,
            unpriced_calls_subscription=self.unpriced_calls_subscription,
            calls_newly_priced=self.calls_newly_priced,
        )

    def add_unpriced(self, row: UnpricedTelemetryRow, *, cost: float | None) -> LedgerBucket:
        is_priced = cost is not None
        added_cost = cost if cost is not None else 0.0
        return LedgerBucket(
            llm_calls=self.llm_calls + 1,
            tokens_in=self.tokens_in + row.tokens_in,
            tokens_out=self.tokens_out + row.tokens_out,
            tokens_cached=self.tokens_cached + row.tokens_cached,
            tokens_reasoning=self.tokens_reasoning + row.tokens_reasoning,
            cost_usd=self.cost_usd + added_cost,
            cost_usd_subscription=self.cost_usd_subscription
            + (added_cost if is_priced and row.is_subscription else 0.0),
            costed_calls=self.costed_calls + (1 if is_priced else 0),
            unpriced_calls=self.unpriced_calls + (0 if is_priced else 1),
            unpriced_calls_subscription=self.unpriced_calls_subscription
            + (1 if not is_priced and row.is_subscription else 0),
            calls_newly_priced=self.calls_newly_priced + (1 if is_priced else 0),
        )


@dataclass(frozen=True)
class ObservationUpdatePlan:
    updates: list[dict[str, object]]
    affected_days: set[tuple[int, date]]
    priced: int
    still_unpriced: int
    added_cost_usd: Decimal
    added_cost_usd_subscription: Decimal
    by_model: tuple[UnpricedBackfillModelSummary, ...]


def quote_safe(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache: CacheTokens,
    *,
    at: datetime,
    prices: PriceBook,
) -> float | None:
    try:
        quoted = quote(model, tokens_in, tokens_out, cache, at=at, prices=prices)
    except ValueError:
        return None
    return None if quoted is None else quoted.cost_usd


def subscription_share(cost: Decimal, *, unpriced_calls: int, unpriced_sub: int) -> Decimal:
    if unpriced_sub <= 0:
        return Decimal(0)
    if unpriced_sub >= unpriced_calls:
        return cost
    return cost * Decimal(unpriced_sub) / Decimal(unpriced_calls)


def collect_ledger_buckets(
    priced_rows: Sequence[PricedLedgerRow],
    unpriced_rows: Sequence[UnpricedTelemetryRow],
    *,
    prices: PriceBook,
) -> dict[tuple[int, str], LedgerBucket]:
    buckets: dict[tuple[int, str], LedgerBucket] = {}
    for row in priced_rows:
        canonical = resolve_harness_catalog_model(row.model, prices) if row.model else ""
        key = (row.agent_id, canonical)
        buckets[key] = buckets.get(key, LedgerBucket()).add_priced(row)

    for urow in unpriced_rows:
        canonical = resolve_harness_catalog_model(urow.model, prices) if urow.model else ""
        cost = (
            quote_safe(
                canonical,
                urow.tokens_in,
                urow.tokens_out,
                CacheTokens(
                    read=urow.tokens_cached,
                    write_5m=urow.cache_write_5m,
                    write_1h=urow.cache_write_1h,
                    write_30m=urow.cache_write_30m,
                ),
                at=urow.ts,
                prices=prices,
            )
            if canonical
            else None
        )
        key = (urow.agent_id, canonical)
        buckets[key] = buckets.get(key, LedgerBucket()).add_unpriced(urow, cost=cost)
    return buckets


def _existing_to_dict(row: ExistingLedgerRow) -> dict[str, object]:
    return {
        "agent_id": row.agent_id,
        "model": row.model,
        "llm_calls": row.llm_calls,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "tokens_cached": row.tokens_cached,
        "tokens_reasoning": row.tokens_reasoning,
        "cost_usd": row.cost_usd,
        "cost_usd_subscription": row.cost_usd_subscription,
        "costed_calls": row.costed_calls,
        "unpriced_calls": row.unpriced_calls,
        "unpriced_calls_subscription": row.unpriced_calls_subscription,
        "estimated_calls": row.estimated_calls,
    }


def _bucket_to_dict(agent_id: int, model: str, bucket: LedgerBucket) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "model": model,
        "llm_calls": bucket.llm_calls,
        "tokens_in": bucket.tokens_in,
        "tokens_out": bucket.tokens_out,
        "tokens_cached": bucket.tokens_cached,
        "tokens_reasoning": bucket.tokens_reasoning,
        "cost_usd": bucket.cost_usd,
        "cost_usd_subscription": bucket.cost_usd_subscription,
        "costed_calls": bucket.costed_calls,
        "unpriced_calls": bucket.unpriced_calls,
        "unpriced_calls_subscription": bucket.unpriced_calls_subscription,
        "estimated_calls": 0,
    }


def _rows_equivalent(existing: Sequence[ExistingLedgerRow], candidate: dict[str, object]) -> bool:
    if len(existing) != 1:
        return False
    ex = existing[0]
    return (
        ex.model == candidate["model"]
        and ex.llm_calls == candidate["llm_calls"]
        and ex.tokens_in == candidate["tokens_in"]
        and ex.tokens_out == candidate["tokens_out"]
        and ex.tokens_cached == candidate["tokens_cached"]
        and ex.tokens_reasoning == candidate["tokens_reasoning"]
        and ex.costed_calls == candidate["costed_calls"]
        and ex.unpriced_calls == candidate["unpriced_calls"]
        and ex.unpriced_calls_subscription == candidate["unpriced_calls_subscription"]
        and ex.estimated_calls == candidate["estimated_calls"]
        and ex.cost_usd == candidate["cost_usd"]
        and ex.cost_usd_subscription == candidate["cost_usd_subscription"]
    )


def _preserve_already_priced(
    ex_rows: Sequence[ExistingLedgerRow],
    bucket: LedgerBucket,
    *,
    only_unpriced_models: bool,
) -> bool:
    if not ex_rows or any(ex.unpriced_calls > 0 for ex in ex_rows):
        return False
    if only_unpriced_models or any(ex.estimated_calls > 0 for ex in ex_rows):
        return True
    return bucket.calls_newly_priced == 0 and bucket.llm_calls <= sum(
        ex.llm_calls for ex in ex_rows
    )


def _should_preserve_existing(
    canonical: str,
    ex_rows: Sequence[ExistingLedgerRow],
    bucket: LedgerBucket,
    *,
    only_unpriced_models: bool,
) -> bool:
    if _preserve_already_priced(ex_rows, bucket, only_unpriced_models=only_unpriced_models):
        return True
    if bucket.llm_calls < sum(ex.llm_calls for ex in ex_rows):
        return True
    return (
        only_unpriced_models
        and bucket.calls_newly_priced == 0
        and all(ex.model == canonical for ex in ex_rows)
    )


def _merge_model_group(
    agent_id: int,
    canonical: str,
    ex_rows: Sequence[ExistingLedgerRow],
    bucket: LedgerBucket | None,
    *,
    only_unpriced_models: bool,
) -> tuple[list[dict[str, object]], bool, int]:
    if bucket is None or _should_preserve_existing(
        canonical, ex_rows, bucket, only_unpriced_models=only_unpriced_models
    ):
        return [_existing_to_dict(ex) for ex in ex_rows], False, 0

    candidate = _bucket_to_dict(agent_id, canonical, bucket)
    if _rows_equivalent(ex_rows, candidate):
        return [_existing_to_dict(ex) for ex in ex_rows], False, 0
    return [candidate], True, bucket.calls_newly_priced


def build_ledger_payload(
    buckets: dict[tuple[int, str], LedgerBucket],
    existing: Sequence[ExistingLedgerRow],
    *,
    prices: PriceBook,
    only_unpriced_models: bool,
) -> tuple[list[int], list[dict[str, object]], int]:
    ex_by_agent: dict[int, dict[str, list[ExistingLedgerRow]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for ex in existing:
        canonical = resolve_harness_catalog_model(ex.model, prices) if ex.model else ""
        ex_by_agent[ex.agent_id][canonical].append(ex)

    new_by_agent: dict[int, dict[str, LedgerBucket]] = defaultdict(dict)
    for (agent_id, canonical), bucket in buckets.items():
        new_by_agent[agent_id][canonical] = bucket

    changed_agents: list[int] = []
    payload: list[dict[str, object]] = []
    total_calls_priced = 0
    for agent_id in sorted(ex_by_agent.keys() | new_by_agent.keys()):
        models = sorted(ex_by_agent[agent_id].keys() | new_by_agent[agent_id].keys())
        agent_rows: list[dict[str, object]] = []
        agent_changed = False
        agent_priced = 0
        for canonical in models:
            rows, changed, priced = _merge_model_group(
                agent_id,
                canonical,
                ex_by_agent[agent_id].get(canonical, ()),
                new_by_agent[agent_id].get(canonical),
                only_unpriced_models=only_unpriced_models,
            )
            agent_rows.extend(rows)
            agent_changed = agent_changed or changed
            agent_priced += priced
        if agent_changed:
            changed_agents.append(agent_id)
            payload.extend(agent_rows)
            total_calls_priced += agent_priced
    return changed_agents, payload, total_calls_priced


def plan_observation_updates(
    obs_rows: Sequence[UnpricedObsRow],
    cache_writes: dict[Decimal, tuple[int, int, int]],
    *,
    book: PriceBook,
) -> ObservationUpdatePlan:
    updates: list[dict[str, object]] = []
    affected_days: set[tuple[int, date]] = set()
    obs_priced = 0
    obs_still_unpriced = 0
    added_cost = Decimal(0)
    added_cost_sub = Decimal(0)
    model_stats: dict[tuple[str, str], _ModelStat] = {}

    for row in obs_rows:
        raw_model = row.model or ""
        canonical = resolve_harness_catalog_model(raw_model, book) if raw_model else ""
        key = (raw_model, canonical)
        stat = model_stats.get(key, _ModelStat())
        w5m, w1h, w30m = cache_writes.get(row.event_id, (0, 0, 0))
        quoted = (
            quote_safe(
                canonical,
                row.tokens_in,
                row.tokens_out,
                CacheTokens(read=row.tokens_cached, write_5m=w5m, write_1h=w1h, write_30m=w30m),
                at=row.occurred_at,
                prices=book,
            )
            if canonical
            else None
        )
        if quoted is None:
            obs_still_unpriced += 1
            model_stats[key] = _ModelStat(
                priced=stat.priced,
                still_unpriced=stat.still_unpriced + 1,
                added_cost_usd=stat.added_cost_usd,
            )
            continue
        cost = Decimal(str(quoted))
        cost_sub = subscription_share(
            cost,
            unpriced_calls=row.unpriced_calls,
            unpriced_sub=row.unpriced_calls_subscription,
        )
        obs_priced += 1
        added_cost += cost
        added_cost_sub += cost_sub
        model_stats[key] = _ModelStat(
            priced=stat.priced + 1,
            still_unpriced=stat.still_unpriced,
            added_cost_usd=stat.added_cost_usd + cost,
        )
        updates.append(
            {
                "event_id": str(row.event_id),
                "model": canonical,
                "cost_usd": str(cost),
                "cost_usd_subscription": str(cost_sub),
                "unpriced_calls": row.unpriced_calls,
                "unpriced_calls_subscription": row.unpriced_calls_subscription,
            }
        )
        affected_days.add((row.agent_id, row.occurred_at.astimezone(UTC).date()))

    summaries = tuple(
        UnpricedBackfillModelSummary(
            raw_model=raw_model,
            canonical_model=canonical,
            observations_priced=stat.priced,
            observations_still_unpriced=stat.still_unpriced,
            added_cost_usd=stat.added_cost_usd,
        )
        for (raw_model, canonical), stat in sorted(model_stats.items())
    )
    return ObservationUpdatePlan(
        updates=updates,
        affected_days=affected_days,
        priced=obs_priced,
        still_unpriced=obs_still_unpriced,
        added_cost_usd=added_cost,
        added_cost_usd_subscription=added_cost_sub,
        by_model=summaries,
    )
