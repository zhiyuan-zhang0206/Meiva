# meiva:note
# Run a planned search or fetch batch against the deps, one thread per item up to the plan's
# concurrency, results in input order. DeepSeek request starts of one batch share a rate limiter.
# The workers return the findings of what they read next to it; the batch hands them to
# `meiva.security.deliver` once, with the process facts the caller read (`delivery`), also when a
# fetch fails after its injection check judged the page.

from __future__ import annotations

import asyncio
from typing import assert_never

from meiva import security
from meiva.sdk_surface.batch import run_batch
from meiva.sdk_surface.pure.media.finding_plan import (
    CarriedFindingsError,
    FindingDelivery,
    Scanned,
    gather,
)
from meiva.web import brave, deepseek, reader
from meiva.web.deps import FetchDeps, SearchDeps, SearchProvider
from meiva.web.plans import FetchPlan, SearchPlan
from meiva.web.transport import RateLimiter
from meiva.web.wire import Hit

__all__ = ["run_fetch", "run_search"]


def run_search(plan: SearchPlan, deps: SearchDeps, delivery: FindingDelivery) -> list[list[Hit]]:
    pacer = RateLimiter(deps.deepseek.qps)

    def search_one(query: str) -> Scanned[list[Hit]]:
        match deps.provider:
            case SearchProvider.BRAVE:
                return brave.search_one(query, plan.count, deps)
            case SearchProvider.DEEPSEEK:
                return deepseek.search_one(query, plan.count, deps, pacer)
            case _:
                assert_never(deps.provider)

    run = gather(asyncio.run(run_batch(plan.queries, search_one, plan.max_concurrent)))
    security.deliver(delivery, run.findings)
    return run.value


# meiva:note
# The answer step's per-invoke bound lives in the provider client (the chat model's timeout),
# see meiva/understand/answer.py for why a batch-layer wait_for would not return early. The
# page-read step has its own web fetch timeout.
def run_fetch(plan: FetchPlan, deps: FetchDeps, delivery: FindingDelivery) -> list[str]:
    pacer = RateLimiter(deps.deepseek.qps)
    try:
        run = gather(
            asyncio.run(
                run_batch(
                    plan.targets,
                    lambda pair: reader.fetch_one(
                        pair[0], pair[1], plan.max_chars, plan.effort, deps, pacer
                    ),
                    plan.max_concurrent,
                )
            )
        )
    except CarriedFindingsError as failed:
        error = security.deliver_kept(delivery, failed)
    else:
        security.deliver(delivery, run.findings)
        return run.value
    raise error
