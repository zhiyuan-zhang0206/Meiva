# meiva:note
# The permissions-helper guards `cli/conftest.py` binds for every CLI test hold. The converge
# door is refused by the root harness's guard, which acts on a door the run has loaded; the
# conftest loads it by importing the helper owner's native-command guard, so this file pins
# that both halves meet.

from __future__ import annotations

from types import ModuleType

import pytest

from base.sessions.tests.refuse_helper import refuse_helper
from services.desktop import permissions_helper
from services.desktop.permissions_helper import launchd_job, lifecycle


def test_a_cli_test_cannot_converge_the_native_helper() -> None:
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        permissions_helper.converge(helper=refuse_helper)


@pytest.mark.parametrize("owner", [lifecycle, launchd_job], ids=lambda owner: owner.__name__)
def test_a_cli_test_cannot_run_a_helper_native_command(owner: ModuleType) -> None:
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        owner.run_bounded(["unreachable"])
