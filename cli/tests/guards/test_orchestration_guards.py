# meiva:note
# The root harness wraps the real converge door without calling it. Refusal/admission behavior
# uses a pure Python door in tests/fixtures/tests/test_helper_door_guard.py. The helper's build,
# signing and launchd modules run a native tool only through the runner they are handed, so a CLI
# test that hands them none cannot reach the host.

from __future__ import annotations

from inspect import unwrap

from services.desktop import permissions_helper


def test_a_cli_test_keeps_the_native_helper_door_wrapped() -> None:
    real = unwrap(permissions_helper.converge)
    assert permissions_helper.converge is not real
    assert real.__module__ == permissions_helper.__name__
