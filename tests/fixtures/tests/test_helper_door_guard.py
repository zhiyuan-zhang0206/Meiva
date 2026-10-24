# meiva:note
# The real door is inspected without calling it. A pure Python fixture door proves collection,
# native admission, failure recovery and repeated-session cleanup without native effects.

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
from inspect import unwrap

import pytest

DOOR = "services.desktop.permissions_helper"


def test_00_the_first_import_inside_the_body_is_wrapped():
    assert DOOR not in sys.modules
    from services.desktop import permissions_helper

    real = unwrap(permissions_helper.converge)
    assert permissions_helper.converge is not real
    assert real.__module__ == DOOR


def test_10_a_loaded_door_stays_wrapped_through_the_name_a_test_binds():
    assert DOOR in sys.modules
    from services.desktop.permissions_helper import converge

    assert converge is not unwrap(converge)
    assert unwrap(converge).__module__ == DOOR


@pytest.mark.native_permissions_helper
def test_20_a_native_proof_preserves_the_wrapped_door_metadata():
    from services.desktop import permissions_helper

    assert permissions_helper.converge.__module__ == DOOR


def test_30_the_next_test_keeps_the_wrapper():
    from services.desktop import permissions_helper

    assert permissions_helper.converge is not unwrap(permissions_helper.converge)
"""

_NATIVE_FIRST = """
import sys
from inspect import unwrap

import pytest

DOOR = "services.desktop.permissions_helper"


@pytest.mark.native_permissions_helper
def test_00_a_native_proof_may_import_the_door_first():
    from services.desktop import permissions_helper

    assert permissions_helper.converge.__module__ == DOOR


def test_10_the_door_it_loaded_stays_wrapped_for_the_next_test():
    from services.desktop import permissions_helper

    assert DOOR in sys.modules
    assert permissions_helper.converge is not unwrap(permissions_helper.converge)
    assert unwrap(permissions_helper.converge).__module__ == DOOR
"""


def test_the_root_guard_wraps_the_first_import_into_the_test_body(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_CASES)

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=4)


def test_a_native_proof_that_imports_first_keeps_the_door_wrapped_for_later_tests(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_NATIVE_FIRST)

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=2)


_SAFE_CONFTEST = (
    _CONFTEST
    + """
def pytest_configure(config):
    from tests.fixtures.guards import HelperDoorGuard
    config.pluginmanager.register(HelperDoorGuard("safe_helper"), "safe-helper-guard")

def pytest_sessionfinish():
    from safe_helper import converge
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        converge(stage="session-finish")
"""
)
_SAFE_DOOR = """
def converge(*, stage):
    return stage
"""
_SAFE_CASES = """
import pytest

from safe_helper import converge
from safe_helper import converge as renamed

with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
    converge(stage="collection")
with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
    renamed(stage="collection-renamed")

@pytest.fixture
def ordinary_window():
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        converge(stage="setup")
    yield
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        converge(stage="teardown")

@pytest.fixture
def native_window():
    assert converge(stage="setup") == "setup"
    yield
    assert converge(stage="teardown") == "teardown"

@pytest.fixture
def failed_setup(native_window):
    raise RuntimeError("deliberate-setup-failure")

@pytest.fixture
def skipped_setup(native_window):
    pytest.skip("deliberate-native-skip")

def test_00_ordinary_body_and_fixtures_refuse(ordinary_window):
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        converge(stage="call")

@pytest.mark.native_permissions_helper
def test_10_native_body_and_fixtures_admit(native_window):
    assert converge(stage="call") == "call"

def test_20_after_native_refuses(ordinary_window):
    test_00_ordinary_body_and_fixtures_refuse(ordinary_window)

@pytest.mark.native_permissions_helper
def test_30_native_setup_failure(failed_setup):
    raise AssertionError("setup must fail first")

def test_40_after_setup_failure_refuses(ordinary_window):
    test_00_ordinary_body_and_fixtures_refuse(ordinary_window)

@pytest.mark.native_permissions_helper
def test_50_native_call_failure(native_window):
    assert converge(stage="failed-call") == "failed-call"
    raise AssertionError("deliberate-call-failure")

def test_60_after_call_failure_refuses(ordinary_window):
    test_00_ordinary_body_and_fixtures_refuse(ordinary_window)

@pytest.mark.native_permissions_helper
def test_70_native_skip(skipped_setup):
    raise AssertionError("setup must skip first")

def test_80_after_skip_refuses(ordinary_window):
    test_00_ordinary_body_and_fixtures_refuse(ordinary_window)
"""


def test_collection_and_test_lifecycle_admit_only_explicit_native_proofs(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest("import pytest\n" + _SAFE_CONFTEST)
    pytester.makepyfile(safe_helper=_SAFE_DOOR, test_lifecycle=_SAFE_CASES)

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=6, failed=1, errors=1, skipped=1)
    assert "deliberate-setup-failure" in result.stdout.str()
    assert "deliberate-call-failure" in result.stdout.str()


@pytest.mark.parametrize("native_first", [False, True], ids=["ordinary-first", "native-first"])
def test_a_first_import_inside_a_test_body_uses_the_native_scope(
    pytester: pytest.Pytester, native_first: bool
) -> None:
    pytester.makeconftest("import pytest\n" + _SAFE_CONFTEST)
    first = (
        """
@pytest.mark.native_permissions_helper
def test_00_first_import():
    assert "safe_helper" not in sys.modules
    from safe_helper import converge as renamed
    assert renamed(stage="native-first") == "native-first"
"""
        if native_first
        else """
def test_00_first_import():
    assert "safe_helper" not in sys.modules
    from safe_helper import converge as renamed
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        renamed(stage="ordinary-first")
"""
    )
    pytester.makepyfile(
        safe_helper=_SAFE_DOOR,
        test_body="import sys\nimport pytest\n"
        + first
        + """
def test_10_the_next_ordinary_test_refuses():
    from safe_helper import converge as renamed
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        renamed(stage="after-first-import")
""",
    )

    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")

    result.assert_outcomes(passed=2)


def test_a_preloaded_door_is_guarded_and_repeated_sessions_restore_it(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest("import pytest\n" + _SAFE_CONFTEST)
    pytester.makepyfile(
        safe_helper=_SAFE_DOOR,
        test_sessions="""
import pytest
from __main__ import preloaded_alias
from safe_helper import converge
from safe_helper import converge as renamed

with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
    converge(stage="collection-after-preload")
with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
    renamed(stage="collection-renamed-after-preload")
with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
    preloaded_alias(stage="preloaded-renamed-alias")

@pytest.mark.native_permissions_helper
def test_native():
    assert converge(stage="native") == "native"
""",
    )
    runner = pytester.makepyfile(
        runner="""
import sys
import pytest
import safe_helper
from safe_helper import converge
from safe_helper import converge as preloaded_alias

original = converge
for attempt in range(2):
    assert pytest.main(["-q", "-o", "addopts=", "test_sessions.py"]) == 0
    from tests.fixtures.guards import HelperDoorGuard
    assert safe_helper.converge is original
    assert converge is original
    assert preloaded_alias is original
    assert converge(stage="outside-session") == "outside-session"
    assert preloaded_alias(stage="outside-session-alias") == "outside-session-alias"
    assert not any(isinstance(finder, HelperDoorGuard) for finder in sys.meta_path)
print("two-sessions-restored")
"""
    )

    result = pytester.runpython(runner)

    assert result.ret == 0
    result.stdout.fnmatch_lines(["two-sessions-restored"])
