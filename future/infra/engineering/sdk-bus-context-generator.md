# SDK event bus: draw from the context's own generator

`meiva.sdk_surface.settings.bus()` builds the SDK's `EventBus` with the generator of the
installed sampling owner (`installed().sampling.rng`). That ties the bus's ACL-retry jitter to
telemetry sampling, widens `SamplingPolicyLike` with an `rng` the sampling policy itself does not
need, and keeps `meiva/sdk_surface/settings.py` in `[roots]` as a middle layer that other modules
import to read the world.

What is left:

- Every production `MeivaContext` root fills `rng` (`MeivaContext.rng`, `require_rng()`):
  `meiva/boot.py` (`launched_context`, `OwnedBoot.new_context`), `agent/execution/child.py`,
  `schedules/jobs/entry.py`, `services/agent_runner/agent_host/lifecycle/configuration.py` and
  `host.py`, `services/wake/schedule_manager/runner.py`. A root whose identifiers must stay unique
  across processes hands in a `random.SystemRandom`.
- `bus()` takes `current_context().require_rng()`; `SamplingPolicyLike.rng` and
  `InertSamplingPolicy.rng` go away, and `settings.py` leaves `[roots]`. The commit that added the
  installation-backed lookup answered the `root-middle-layer` hint with this item.
- An AST test asserts that every non-test `MeivaContext(` call that passes `clock_factory=` also
  passes `rng=`, so a root that forgets the generator fails in CI instead of at the first
  `require_rng()`.

This document changes no runtime behavior.
