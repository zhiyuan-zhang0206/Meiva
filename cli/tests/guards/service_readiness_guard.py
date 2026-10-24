# meiva:note
# The CLI owner's readiness guard, bound beside its only consumers (`cli/conftest.py` and
# `tests/path_scoped/cli_tests.py`). It lives here, not in the root harness, because it
# imports `cli.commands.probe`: a root-level import would put the CLI probe package and the
# whole supervision roster behind it into every test's dependency set.

import pytest

from cli.commands.probe import ReadinessWait

__all__ = ["guard_service_readiness"]


@pytest.fixture
def guard_service_readiness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI owner's guard: `cli.commands.lifecycle.root_readiness.wait_for_service_tree` reports every
    service ready without polling anything.

    The start path's readiness wait is bounded by `SERVICE_READY_TIMEOUT_S` (180 s),
    and a test that reaches it with real probes would sit there for the full bound —
    long enough under `-n auto` for the worker to be declared down, which reports as
    an infrastructure failure rather than as the stubbing gap it is. Reporting no
    unready specs also keeps such a test's `meiva start` at rc 0 instead of the
    readiness code.

    Opt out with `@pytest.mark.real_service_readiness_gate` when the wait or the exit
    code it produces is the subject (cli/commands/lifecycle/tests/startup/test_start_live_path.py)."""
    if request.node.get_closest_marker("real_service_readiness_gate"):
        return
    ready = ReadinessWait((), 0.0, sessions_gone=False)
    monkeypatch.setattr(
        "cli.commands.lifecycle.root_readiness.wait_for_service_tree",
        lambda *_a, **_kw: ready,
    )
