# meiva:note
# The root harness wraps the real converge door without calling it, and the helper's native
# commands retain the owner's guard that the CLI conftest imports. Refusal/admission behavior
# uses a pure Python door in tests/fixtures/tests/test_helper_door_guard.py.

from __future__ import annotations

from inspect import unwrap
from types import ModuleType

import pytest

from services.desktop import permissions_helper
from services.desktop.permissions_helper import launchd_job, lifecycle


def test_a_cli_test_keeps_the_native_helper_door_wrapped() -> None:
    real = unwrap(permissions_helper.converge)
    assert permissions_helper.converge is not real
    assert real.__module__ == permissions_helper.__name__


@pytest.mark.parametrize("owner", [lifecycle, launchd_job], ids=lambda owner: owner.__name__)
def test_a_cli_test_cannot_run_a_helper_native_command(owner: ModuleType) -> None:
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        owner.run_bounded(["unreachable"])
