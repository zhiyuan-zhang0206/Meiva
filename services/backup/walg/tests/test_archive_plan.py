from __future__ import annotations

import shlex
from pathlib import Path

from services.backup.walg.archive_plan import (
    ARCHIVE_TIMEOUT_S,
    ActualArchive,
    ExpectedArchive,
    archive_args,
    differs,
    expected_settings,
    mismatch_message,
    postgres_command,
)

_CONFIG = Path("/homes/a b%p/walg.json")
_BINARY = Path("/homes/a b%p/runtime/walg/wal-g")


def test_a_percent_in_either_path_is_escaped_for_postgres_and_the_arguments_are_not() -> None:
    command = postgres_command(_CONFIG, _BINARY, "wal-fetch", "%f", "%p")

    assert shlex.split(command) == [
        "/homes/a b%%p/runtime/walg/wal-g",
        "--config",
        "/homes/a b%%p/walg.json",
        "wal-fetch",
        "%f",
        "%p",
    ]


def test_the_expected_settings_archive_every_minute_through_the_pinned_binary() -> None:
    expected = expected_settings(_CONFIG, _BINARY)

    assert (expected.mode, expected.timeout_s) == ("on", ARCHIVE_TIMEOUT_S)
    assert shlex.split(expected.command)[-2:] == ["wal-push", "%p"]
    assert archive_args(expected) == [
        "-c",
        "archive_mode=on",
        "-c",
        f"archive_timeout={ARCHIVE_TIMEOUT_S}s",
        "-c",
        f"archive_command={expected.command}",
    ]


def _actual(expected: ExpectedArchive, **changes: object) -> ActualArchive:
    fields: dict[str, object] = {
        "mode": expected.mode,
        "timeout_s": expected.timeout_s,
        "command": expected.command,
    }
    fields.update(changes)
    return ActualArchive(
        mode=str(fields["mode"]),
        timeout_s=int(str(fields["timeout_s"])),
        command=str(fields["command"]),
    )


def test_matching_settings_do_not_differ() -> None:
    expected = expected_settings(_CONFIG, _BINARY)

    assert not differs(_actual(expected), expected)


def test_any_one_changed_setting_differs() -> None:
    expected = expected_settings(_CONFIG, _BINARY)

    assert differs(_actual(expected, mode="off"), expected)
    assert differs(_actual(expected, timeout_s=expected.timeout_s + 1), expected)
    assert differs(_actual(expected, command="/bin/false %p"), expected)


def test_the_mismatch_names_the_machine_and_both_value_sets() -> None:
    expected = expected_settings(_CONFIG, _BINARY)

    message = mismatch_message(
        "macmini", _actual(expected, mode="off", command="/bin/false %p"), expected
    )

    assert "macmini" in message
    assert "archive_mode=off (expected on)" in message
    assert "'/bin/false %p'" in message and repr(expected.command) in message
    assert "meiva stop" in message
