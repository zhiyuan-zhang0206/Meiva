import pytest

from agent.graph.interrupt import InterruptEvent as InterruptEvent
from agent.graph.llm.tests.cancel_fixture import CancelPool as CancelPool


@pytest.fixture
def fake_cancel_event() -> InterruptEvent:
    return InterruptEvent()
