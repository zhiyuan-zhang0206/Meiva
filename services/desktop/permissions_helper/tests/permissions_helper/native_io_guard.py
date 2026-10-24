# meiva:note
# The helper owner's native-command guard, bound beside its consumers: the helper's own tests
# (`tests/conftest.py`) and every CLI test (`cli/conftest.py`, `tests/path_scoped/cli_tests.py`).
# It lives with the helper, not in the root harness, because it imports `launchd_job` and
# `lifecycle`: a root-level import would put the helper's build, signing and launchd modules
# (and the Swift source, Info.plist and entitlement paths `lifecycle` names) into every test's
# dependency set.

import pytest

from services.desktop.permissions_helper import launchd_job, lifecycle

__all__ = ["permissions_helper_native_io"]


@pytest.fixture
def permissions_helper_native_io(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protect direct owner-private native calls beside helper and CLI consumers."""
    if "native_permissions_helper" in request.keywords:
        return

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("permissions helper native effect forbidden: mock its native command boundary")

    monkeypatch.setattr(lifecycle, "run_bounded", forbidden)
    monkeypatch.setattr(launchd_job, "run_bounded", forbidden)
