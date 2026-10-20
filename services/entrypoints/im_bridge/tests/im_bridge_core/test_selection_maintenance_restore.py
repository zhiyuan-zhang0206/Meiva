import pytest

from base.deploy.maintenance import pause_owner
from services.entrypoints.im_bridge.state import save_switch_state
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeGateway,
    create_test_core,
)


async def test_restoration_propagates_unknown_selection_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as tasks:
        save_switch_state({"telegram:chat": 7})
        core = create_test_core(FakeGateway(), tasks=tasks)

        async def broken_account() -> str:
            raise RuntimeError("unexpected account fault")

        monkeypatch.setattr(core.adapters["telegram"], "outbound_account_id", broken_account)
        with pytest.raises(RuntimeError, match="unexpected account fault"):
            await core.restore_subscriptions()
        assert core._subscriptions == {}


async def test_restoration_rejects_invalid_maintenance_journal() -> None:
    async with owned_tasks() as tasks:
        path = pause_owner.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"state":"paused","maintenance":{"phase":"unknown"}}')
        core = create_test_core(FakeGateway(), tasks=tasks)
        with pytest.raises(RuntimeError, match="unreadable pause owner"):
            await core.restore_subscriptions()
        assert core._subscriptions == {}
