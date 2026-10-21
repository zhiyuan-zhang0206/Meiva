# meiva:note
# The stop records the browser's tabs before the phase that stops the browser.
# The order lives in `stop()` itself, so it is asserted on that function's own
# source: the record must be the first thing the stop does, and it must sit behind
# the `keep_browser` choice. `cli/commands/lifecycle/tests/stop` is strict about
# patching first-party code, so this pins the invariant without a fake stop.

from __future__ import annotations

import inspect

from cli.commands.lifecycle import _temporary_stop as command


def _stop_source() -> str:
    return inspect.getsource(command.stop)


# meiva:note
# The services phase is what signals `meiva-browser`; recording after it would
# find Chrome already gone.
def test_the_browser_session_is_recorded_before_the_services_phase() -> None:
    source = _stop_source()
    record = source.index("record_browser_session")
    services = source.index("_stop_selected(")
    assert record < services


def test_the_record_is_behind_the_keep_browser_choice() -> None:
    source = _stop_source()
    guard = source.index("if not keep_browser:")
    record = source.index("record_browser_session")
    services = source.index("_stop_selected(")
    assert guard < record < services


def test_the_record_is_a_timed_phase_so_a_slow_worker_cannot_vanish() -> None:
    assert '_timed_phase(phases, "browser-session", record_browser_session)' in _stop_source()
