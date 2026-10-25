from __future__ import annotations

from pathlib import Path

from services.desktop.computer import ocr


def test_the_binary_path_follows_the_variable_after_import(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert ocr.bin_path(first) == first / "logs" / "computer" / "ocr-bin" / "ocr"
    assert ocr.bin_path(second) == second / "logs" / "computer" / "ocr-bin" / "ocr"
