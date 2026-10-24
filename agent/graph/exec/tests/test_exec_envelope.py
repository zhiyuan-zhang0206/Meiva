from pathlib import Path

import pytest

from agent.execution.protocol import ResultPayload, write_result
from agent.graph.exec._child_boundary import payload_without_envelope
from agent.graph.exec._subprocess import read_result_envelope


def test_a_watchdog_exit_without_an_envelope_reads_as_an_unsignalled_timeout() -> None:
    payload = payload_without_envelope(124)

    assert payload is not None
    assert (payload.kind, payload.exc_type) == ("timed_out", "TimeoutError")
    assert payload.exc_msg is not None and "watchdog" in payload.exc_msg


@pytest.mark.parametrize("returncode", [None, 0, 1, -9])
def test_any_other_exit_without_an_envelope_is_left_to_the_callers_flags(
    returncode: int | None,
) -> None:
    assert payload_without_envelope(returncode) is None


def test_a_written_envelope_is_read_back(tmp_path: Path) -> None:
    path = tmp_path / "res.json"
    write_result(path, ResultPayload(kind="done", code_reached=True))

    payload, error = read_result_envelope(path, 0)

    assert error is None
    assert payload is not None and (payload.kind, payload.code_reached) == ("done", True)


def test_a_missing_envelope_after_a_watchdog_exit_is_the_watchdog_payload(tmp_path: Path) -> None:
    payload, error = read_result_envelope(tmp_path / "absent.json", 124)

    assert error is None
    assert payload is not None and payload.kind == "timed_out"


def test_a_missing_envelope_after_any_other_exit_has_neither_payload_nor_error(
    tmp_path: Path,
) -> None:
    assert read_result_envelope(tmp_path / "absent.json", -9) == (None, None)


def test_a_malformed_envelope_comes_back_as_an_error_instead_of_a_guessed_outcome(
    tmp_path: Path,
) -> None:
    path = tmp_path / "res.json"
    path.write_text('{"v": 1, "kind": "exploded"}', encoding="utf-8")

    payload, error = read_result_envelope(path, 0)

    assert payload is None
    assert error is not None and error.startswith("exec result envelope could not be decoded:")
