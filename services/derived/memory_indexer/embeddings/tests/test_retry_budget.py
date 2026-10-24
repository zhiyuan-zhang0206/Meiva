from dataclasses import replace

from base.host.net.resilience import MAX_RETRY_AFTER_RESPECT_S, ExponentialBackoff, Policy
from services.derived.memory_indexer.embeddings import factory, gemini

TIMEOUT_S = 60.0


def _assert_coverage(policy: Policy, timeout_s: float) -> float:
    worst_batch = policy.max_attempts * timeout_s
    worst_batch += sum(
        max(policy.backoff(attempt), MAX_RETRY_AFTER_RESPECT_S) + 2 * policy.jitter_span
        for attempt in range(policy.max_attempts - 1)
    )
    provider_budget = gemini.worst_case_batch_seconds(timeout_s, policy)
    assert provider_budget >= worst_batch
    return provider_budget


def test_batch_budget_covers_retry_policy() -> None:
    original = _assert_coverage(gemini.EMBED_POLICY, TIMEOUT_S)
    assert factory.worst_case_batch_seconds("gemini", timeout_seconds=TIMEOUT_S) == original

    slower = replace(gemini.EMBED_POLICY, backoff=ExponentialBackoff(base=100, factor=2, cap=1000))
    _assert_coverage(slower, TIMEOUT_S)
    assert _assert_coverage(gemini.EMBED_POLICY, TIMEOUT_S + 300.0) > original
