import pytest

from agent.execution.delivery_plan import delivers_telemetry, rewrites_result_after_delivery_failure


@pytest.mark.parametrize(
    ("kind", "delivers", "rewrites"),
    [
        ("done", True, True),
        ("lifecycle", True, True),
        ("crashed", True, False),
        ("cancelled", False, False),
        ("timed_out", False, False),
    ],
)
def test_a_failed_delivery_rewrites_a_success_but_never_replaces_a_crash(
    kind: str, delivers: bool, rewrites: bool
) -> None:
    assert delivers_telemetry(kind) is delivers
    assert rewrites_result_after_delivery_failure(kind) is rewrites
