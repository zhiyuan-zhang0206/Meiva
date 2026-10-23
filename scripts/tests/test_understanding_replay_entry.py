from pathlib import Path
from unittest.mock import Mock

import pytest

from base.db.code_version_gate import ProcessDbGate
from base.native_process.loaded_commit import LoadedCommit
from scripts.verify import understanding_replay as replay


def test_report_uses_one_captured_gated_database(tmp_path: Path) -> None:
    loaded = LoadedCommit(tmp_path, "captured-before-move")
    capture = Mock(return_value=loaded)
    count = Mock(return_value=7)
    database = Mock()
    create = Mock(return_value=database)
    report = Mock()

    class _Version:
        def __init__(self, image: LoadedCommit) -> None:
            assert image is loaded

        def get(self) -> int:
            return int(count(loaded.source_root, loaded.sha))

    replay.main(
        ["report", "42"],
        require_preview_fn=Mock(),
        config_cls=Mock(return_value=Mock()),
        capture_fn=capture,
        version_cls=_Version,
        process_name_fn=lambda: "preview",
        db_from_settings_fn=create,
        report_fn=report,
    )
    capture.assert_called_once_with()
    create.assert_called_once()
    report.assert_called_once_with(database, 42)
    count.assert_not_called()
    gate = create.call_args.kwargs["gate"]
    assert isinstance(gate, ProcessDbGate)
    assert gate.application_name() == "meiva:preview:v7"
    count.assert_called_once_with(tmp_path, "captured-before-move")
    gate.observe_minimum(7)
    assert not gate.min_read_due()


def test_preview_refusal_precedes_capture_and_database(tmp_path: Path) -> None:
    capture = Mock()
    create = Mock()
    with pytest.raises(SystemExit, match="preview container only"):
        replay.main(
            ["report", "42"],
            dockerenv=tmp_path / "missing",
            capture_fn=capture,
            db_from_settings_fn=create,
        )
    capture.assert_not_called()
    create.assert_not_called()
