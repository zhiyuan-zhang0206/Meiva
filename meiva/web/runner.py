# meiva:note
# Run a planned search or fetch batch against the deps, one thread per item up to the plan's
# concurrency, results in input order. DeepSeek request starts of one batch share a rate limiter.

from __future__ import annotations

import asyncio

from meiva.sdk_surface.batch import run_batch
from meiva.web import brave, deepseek, reader
from meiva.web.deps import FetchDeps, SearchDeps
from meiva.web.plans import FetchPlan, SearchPlan
from meiva.web.transport import RateLimiter
from meiva.web.wire import Hit

__all__ = ["run_fetch", "run_search"]


def run_search(plan: SearchPlan, deps: SearchDeps) -> list[list[Hit]]:
    pacer = RateLimiter(deps.deepseek.qps)

    def search_one(query: str) -> list[Hit]:
        if deps.provider == "brave":
            return brave.search_one(query, plan.count, deps)
        return deepseek.search_one(query, plan.count, deps, pacer)

    return asyncio.run(run_batch(plan.queries, search_one, plan.max_concurrent))


# meiva:note
# The answer step's per-invoke bound lives in the provider client (the chat model's timeout),
# see meiva/understand/answer.py for why a batch-layer wait_for would not return early. The
# page-read step has its own web fetch timeout.
def run_fetch(plan: FetchPlan, deps: FetchDeps) -> list[str]:
    pacer = RateLimiter(deps.deepseek.qps)
    return asyncio.run(
        run_batch(
            plan.targets,
            lambda pair: reader.fetch_one(
                pair[0], pair[1], plan.max_chars, plan.effort, deps, pacer
            ),
            plan.max_concurrent,
        )
    )
