from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner

__all__ = ["fixed_sampling_owner"]


# meiva:note
# A sampling owner whose first snapshot and every refresh are the same policy: the SDK call path
# under test sees live sampling without a network call, a settings read or a `.env` file.
def fixed_sampling_owner(policy: SamplingPolicy | None = None) -> SamplingPolicyOwner:
    fixed = SamplingPolicy() if policy is None else policy
    return SamplingPolicyOwner(initial=fixed, fetch=lambda: fixed)
