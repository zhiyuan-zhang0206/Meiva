import pytest

from services.agent_runner.agent_host.lifecycle.lease_controls import require_lifecycle_claim


@pytest.mark.parametrize("durable", [[], [True]])
def test_no_claim_or_one_lifecycle_command_is_accepted(durable: list[bool]) -> None:
    require_lifecycle_claim(durable)


@pytest.mark.parametrize("durable", [[False], [True, True], [True, False]])
def test_anything_else_in_the_claim_stops_the_wake(durable: list[bool]) -> None:
    with pytest.raises(RuntimeError, match="lease control claim returned an unaccepted command"):
        require_lifecycle_claim(durable)
