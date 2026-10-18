from __future__ import annotations

from pathlib import Path

import pytest

from base.events import scan_kinds


def test_an_unreadable_source_is_named_not_silently_unscanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    good = package / "good.py"
    good.write_text("logger.info('x', event='ok')\n", encoding="utf-8")
    unreadable = package / "locked.py"
    unreadable.write_text("logger.info('x', event='hidden')\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        scanned = dict(scan_kinds.walk_py(tmp_path))
    finally:
        unreadable.chmod(0o600)

    assert "pkg/good.py" in scanned
    assert "pkg/locked.py" not in scanned
    assert "pkg/locked.py could not be read" in capsys.readouterr().err
