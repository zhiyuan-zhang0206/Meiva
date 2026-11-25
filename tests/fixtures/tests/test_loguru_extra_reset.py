import pytest


def test_loguru_extra_bound_by_a_test_does_not_reach_the_next_test(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
pytest_plugins = ["tests.fixtures.env_bootstrap", "tests.fixtures.guards"]
"""
    )
    pytester.makepyfile(
        """
from loguru import logger


def test_a_binds_agent() -> None:
    logger.configure(extra={"agent_id": "-"})


def test_b_sees_unbound_extra() -> None:
    records: list[dict[str, object]] = []
    sink = logger.add(lambda message: records.append(message.record["extra"]), level="INFO")
    try:
        logger.info("probe", event="probe")
    finally:
        logger.remove(sink)
    assert records == [{"event": "probe"}]
"""
    )
    pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider").assert_outcomes(
        passed=2
    )
