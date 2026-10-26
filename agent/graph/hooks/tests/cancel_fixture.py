import pytest

from agent.graph.interrupt import InterruptEvent


@pytest.fixture
def fake_cancel_event() -> InterruptEvent:
    return InterruptEvent()
