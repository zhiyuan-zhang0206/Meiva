# meiva:note
# The process-level trace state a test starts from. `tracing.state` is the module's one guard
# for the idempotent init and the collector retry; a test that arms recording leaves it set.

from base.telemetry import tracing


def reset_tracing_state() -> None:
    tracing.state.clear()
    tracing.state.update(
        closed=False,
        collector_offline_reported=False,
        retry_owner=None,
        arm_owner=None,
        traceloop_init=None,
    )
