# SDK event bus: draw from the context's own generator

`meiva.sdk_surface.settings.bus()` builds the SDK's `EventBus` with the generator of the
installed sampling owner (`installed().sampling.rng`). That ties the bus's ACL-retry jitter to
telemetry sampling, widens `SamplingPolicyLike` with an `rng` the sampling policy itself does not
need, and keeps `meiva/sdk_surface/settings.py` in `[roots]` as a middle layer that other modules
import to read the world.

What is left:

- Every production `MeivaContext` root fills `rng` (`MeivaContext.rng`, `require_rng()`),
  `meiva_home` and `user_home`. Done: `agent/execution/child.py`, `schedules/jobs/entry.py`,
  the agent host (`host.py`, with `HostPolicy.user_home`) and
  `services/wake/schedule_manager/runner.py`. Left: `meiva/boot.py` (`launched_context`,
  `OwnedBoot.new_context`). A root that needs the generator unpredictable and not copied by a
  fork hands in a `random.SystemRandom`.
- `require_new_id()` returns `new_uuid(rng).hex` (`base/clock/ids.py`, the 32-character form
  `CommandRuntime.new_id()` and the IM bridge use) once that module is on main.
- `bus()` takes `current_context().require_rng()`; `SamplingPolicyLike.rng` and
  `InertSamplingPolicy.rng` go away, and `settings.py` leaves `[roots]`. The commit that added the
  installation-backed lookup answered the `root-middle-layer` hint with this item.
- An AST test asserts that every non-test `MeivaContext(` (or `replace(context, ...)`) call that
  passes `clock_factory=` also passes `rng=`, `user_home=` and `meiva_home=`, so a root that
  forgets one fails in CI instead of at the first `require_*()`. It lands with the last root.

This document changes no runtime behavior.
