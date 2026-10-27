# SDK event bus: sampling owner no longer carries the bus generator

`meiva.sdk_surface.settings.bus()` used to build the SDK's `EventBus` with the generator of the
installed sampling owner (`installed().sampling.rng`). It now answers the bound context's own bus
(`current_context().require_bus()`): a root that holds a bus passes it as `MeivaContext.bus`, and a
root that must keep the Redis stack off its import path passes `bus_factory=deferred_event_bus(config,
rng=rng)`, which builds one bus over the root's configuration on the first read. Every production
`MeivaContext` root fills `rng`, `meiva_home`, `user_home` and one of the two bus forms:
`agent/execution/child.py`, `schedules/jobs/entry.py` and `services/wake/schedule_manager/runner.py`
(factory), the agent host (`host.py`, the bus it owns) and `meiva/boot.py` (`launched_context` and
`OwnedBoot.new_context`, factory). `meiva/sdk_surface/settings.py` left `[roots]` for the `bus` kind.

What is left:

- Nothing outside tests reads `SamplingPolicyLike.rng` or `InertSamplingPolicy.rng` any more, so the
  property can go away with the sampling owner's `rng` attribute; the sampling decision draws from
  the owner's own generator.
- `require_new_id()` returns the 32-character hex form of `new_uuid(rng)`. Its body repeats that
  function; `base/clock/ids.py` (with `CommandRuntime.new_id()`) is on main, and the body becomes that
  import.
- An AST test asserts that every non-test `MeivaContext(` (or `replace(context, ...)`) call that
  passes `clock_factory=` also passes `rng=`, `user_home=`, `meiva_home=` and a `bus=` or
  `bus_factory=`, so a root that forgets one fails in CI instead of at the first `require_*()`.

This document changes no runtime behavior.
