# meiva:note
# The auth-retry a test hands to a publisher or client it builds without a bus: its generator is
# seeded, so a test that depends on the retry jitter is repeatable.

from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.redis_resilience import DEFAULT_AUTH_RETRY_POLICY, AuthRetry, AuthRetryPolicy

__all__ = ["seeded_auth_retry"]


def seeded_auth_retry(policy: AuthRetryPolicy = DEFAULT_AUTH_RETRY_POLICY) -> AuthRetry:
    return AuthRetry(policy, rng=seeded_rng())
