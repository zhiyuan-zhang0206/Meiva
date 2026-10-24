# meiva:note
# The permissions-helper door is refused the moment a test run first imports it, whichever test
# imports it. The harness only names the door, so each case runs in a child pytest whose
# conftest loads the root guards and never imports the door itself.

import pytest

_CONFTEST = """
pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.plugin_registrations",
    "tests.fixtures.guards",
]
"""

_CASES = """
import sys

import pytest

from base.sessions.tests.refuse_helper import refuse_helper

DOOR = "services.desktop.permissions_helper"


def test_00_the_first_import_inside_the_body_is_refused():
    assert DOOR not in sys.modules
    from services.desktop import permissions_helper

    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        permissions_helper.converge(helper=refuse_helper)


def test_10_a_loaded_door_is_refused_through_the_name_a_test_binds():
    assert DOOR in sys.modules
    from services.desktop.permissions_helper import converge

    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        converge(helper=refuse_helper)


@pytest.mark.native_permissions_helper
def test_20_a_native_proof_sees_the_real_door_after_the_refusal_was_undone():
    from services.desktop import permissions_helper

    assert permissions_helper.converge.__module__ == DOOR


def test_30_the_next_test_is_refused_again():
    from services.desktop import permissions_helper

    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        permissions_helper.converge(helper=refuse_helper)
"""

_NATIVE_FIRST = """
import sys

import pytest

DOOR = "services.desktop.permissions_helper"


@pytest.mark.native_permissions_helper
def test_00_a_native_proof_may_import_the_door_first():
    from services.desktop import permissions_helper

    assert permissions_helper.converge.__module__ == DOOR


def test_10_the_door_it_loaded_is_refused_for_the_next_test():
    from services.desktop import permissions_helper

    assert DOOR in sys.modules
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        permissions_helper.converge(helper=lambda: None)
"""


def test_the_refusal_follows_the_first_import_into_the_test_body(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_CASES)

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=4)


def test_a_native_proof_that_imports_first_leaves_the_door_refused_for_later_tests(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_NATIVE_FIRST)

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=2)
