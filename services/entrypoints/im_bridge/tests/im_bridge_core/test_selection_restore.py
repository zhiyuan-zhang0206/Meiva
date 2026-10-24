from pathlib import Path

import pytest

from services.entrypoints.im_bridge.state import save_switch_state
from services.entrypoints.im_bridge.tests.support import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    CoreFactory,
    FakeGateway,
    FakeTypingAdapter,
)
from services.entrypoints.im_bridge.tests.test_im_bridge_core import make_core as make_core


class _BrokenAccountAdapter(FakeTypingAdapter):
    async def outbound_account_id(self) -> str:
        raise RuntimeError("unexpected account fault")


async def test_restoration_propagates_unknown_selection_fault(
    make_core: CoreFactory, state_dir: Path
) -> None:
    async with owned_tasks() as tasks:
        save_switch_state(state_dir, {"telegram:chat": 7})
        core = make_core(FakeGateway(), tasks=tasks)
        core.register(_BrokenAccountAdapter())
        with pytest.raises(RuntimeError, match="unexpected account fault"):
            await core.restore_subscriptions()
        assert core._subscriptions == {}
