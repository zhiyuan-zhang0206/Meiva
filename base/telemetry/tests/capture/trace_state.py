# meiva:note
# The process-level trace state a test starts from. `tracing.state` is the module's one guard
# for the idempotent init and the collector retry; a test that arms recording leaves it set.

from base.telemetry import tracing


def reset_tracing_state() -> None:
    tracing.state["closed"] = False
    tracing.state["collector_offline_reported"] = False
    tracing.state["retry_owner"] = None
    tracing.state["arm_owner"] = None
    tracing.state["traceloop_init"] = None
