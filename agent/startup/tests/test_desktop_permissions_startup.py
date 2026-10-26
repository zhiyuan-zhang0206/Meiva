from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from agent.startup import notify_desktop_permissions_at_startup
from base.host.converge.accessibility import (
    AccessibilityState,
    AccessibilityStatus,
)
from base.host.converge.accessibility import (
    read_status as read_accessibility_status,
)
from base.host.converge.accessibility import (
    status_file_path as accessibility_status_file_path,
)
from base.host.converge.accessibility import (
    write_status as write_accessibility_status,
)
from base.host.converge.screen_capture import (
    ScreenCaptureState,
    ScreenCaptureStatus,
)
from base.host.converge.screen_capture import (
    read_status as read_screen_capture_status,
)
from base.host.converge.screen_capture import (
    status_file_path as screen_capture_status_file_path,
)
from base.host.converge.screen_capture import (
    write_status as write_screen_capture_status,
)
from meiva.sdk_surface import plugins


@pytest.fixture
def home(unit_home: Path) -> Path:
    return unit_home


_SCREEN_FAULT = ScreenCaptureStatus(
    state=ScreenCaptureState.NO_GRANT,
    diagnostic="The permissions helper holds no Screen Recording grant. Fix: System Settings.",
)
_ACCESSIBILITY_FAULT = AccessibilityStatus(
    state=AccessibilityState.NOT_GRANTED,
    diagnostic="The permissions helper holds no Accessibility grant. Fix: System Settings.",
)


class _FakeUI:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.keys: list[str] = []

    def notify(self, **kwargs: object) -> int:
        key = kwargs.pop("idempotency_key")
        assert isinstance(key, str) and 1 <= len(key) <= 128
        self.keys.append(key)
        self.calls.append(kwargs)
        return 1


class _FailingUI:
    def notify(self, **kwargs: object) -> int:
        raise RuntimeError("gateway unreachable")


@pytest.fixture
def fake_ui() -> Iterator[_FakeUI]:
    ui = _FakeUI()
    added = plugins.install_member("startup-test", "ui", "notify", ui.notify)
    yield ui
    plugins.uninstall(added)


@pytest.fixture
def failing_ui() -> Iterator[None]:
    added = plugins.install_member("startup-test", "ui", "notify", _FailingUI().notify)
    yield
    plugins.uninstall(added)


class TestNotifyDesktopPermissionsAtStartup:
    @pytest.mark.asyncio
    async def test_noop_when_no_status_file(self, fake_ui: _FakeUI, home: Path):
        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert fake_ui.calls == []

    @pytest.mark.asyncio
    async def test_the_key_it_is_given_is_the_key_the_notice_carries(
        self, fake_ui: _FakeUI, home: Path
    ):
        write_screen_capture_status(home, _SCREEN_FAULT)

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=7))

        assert fake_ui.keys == [str(UUID(int=7))]

    @pytest.mark.asyncio
    async def test_screen_only_fault_keeps_the_existing_notice_shape(
        self, fake_ui: _FakeUI, home: Path
    ):
        write_screen_capture_status(home, _SCREEN_FAULT)

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert fake_ui.calls == [
            {
                "title": _SCREEN_FAULT.headline,
                "content": _SCREEN_FAULT.diagnostic,
                "priority": "P1",
            }
        ]
        assert read_screen_capture_status(home) is None
        assert read_accessibility_status(home) is None

    @pytest.mark.asyncio
    async def test_accessibility_only_fault_notifies_and_clears(self, fake_ui: _FakeUI, home: Path):
        write_accessibility_status(home, _ACCESSIBILITY_FAULT)

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert fake_ui.calls == [
            {
                "title": _ACCESSIBILITY_FAULT.headline,
                "content": _ACCESSIBILITY_FAULT.diagnostic,
                "priority": "P1",
            }
        ]
        assert read_accessibility_status(home) is None

    @pytest.mark.asyncio
    async def test_both_faults_post_one_combined_notice(self, fake_ui: _FakeUI, home: Path):
        write_screen_capture_status(home, _SCREEN_FAULT)
        write_accessibility_status(home, _ACCESSIBILITY_FAULT)

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert fake_ui.calls == [
            {
                "title": "Desktop permissions missing",
                "content": (
                    f"{_SCREEN_FAULT.headline}: {_SCREEN_FAULT.diagnostic}\n\n"
                    f"{_ACCESSIBILITY_FAULT.headline}: {_ACCESSIBILITY_FAULT.diagnostic}"
                ),
                "priority": "P1",
            }
        ]
        assert read_screen_capture_status(home) is None
        assert read_accessibility_status(home) is None

    @pytest.mark.asyncio
    async def test_notify_failure_restores_both_claims(self, failing_ui: None, home: Path):
        write_screen_capture_status(home, _SCREEN_FAULT)
        write_accessibility_status(home, _ACCESSIBILITY_FAULT)

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert read_screen_capture_status(home) == _SCREEN_FAULT
        assert read_accessibility_status(home) == _ACCESSIBILITY_FAULT
        assert not screen_capture_status_file_path(home).with_suffix(".processing").exists()
        assert not accessibility_status_file_path(home).with_suffix(".processing").exists()

    @pytest.mark.asyncio
    async def test_concurrent_claim_loser_skips_only_that_status_file(
        self, fake_ui: _FakeUI, home: Path
    ):
        write_screen_capture_status(home, _SCREEN_FAULT)
        write_accessibility_status(home, _ACCESSIBILITY_FAULT)
        screen_capture_status_file_path(home).rename(
            screen_capture_status_file_path(home).with_suffix(".processing")
        )

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert fake_ui.calls == [
            {
                "title": _ACCESSIBILITY_FAULT.headline,
                "content": _ACCESSIBILITY_FAULT.diagnostic,
                "priority": "P1",
            }
        ]
        assert screen_capture_status_file_path(home).with_suffix(".processing").exists()
        assert read_accessibility_status(home) is None

    @pytest.mark.asyncio
    async def test_available_statuses_are_cleaned_without_notifying(
        self, fake_ui: _FakeUI, home: Path
    ):
        write_screen_capture_status(home, ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE))
        write_accessibility_status(home, AccessibilityStatus(state=AccessibilityState.GRANTED))

        await notify_desktop_permissions_at_startup(home, idempotency_key=UUID(int=1))

        assert fake_ui.calls == []
        assert read_screen_capture_status(home) is None
        assert read_accessibility_status(home) is None
