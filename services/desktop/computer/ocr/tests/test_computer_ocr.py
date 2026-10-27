"""Unit tests for services/desktop/computer/ocr/__init__.py — the Vision-based screen OCR.

Covers the TSV parsing, coordinate passthrough (the Swift binary already
emits physical pixels, top-left origin), soft-failure surface (OcrError on
build/run problems), and the build-on-demand binary cache. The Swift
recognition itself is exercised live on the preview cluster, not in unit
tests (same stance as the permissions-helper tests). The subprocess runs go
through `base.host.proc.run_bounded` — its own tests own the bounded-kill
behavior; here the seam is patched.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from base.paths import meiva_home
from services.desktop.computer import ocr as ocr_mod
from services.desktop.computer.ocr import OcrError, ocr_image


def _script(path: Path, body: str, *, fresh: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    mtime = (
        max(time.time(), ocr_mod.SOURCE.stat().st_mtime) + 60.0
        if fresh
        else ocr_mod.SOURCE.stat().st_mtime - 60.0
    )
    os.utime(path, (mtime, mtime))
    return path


def _stale_pair(tmp_path: Path, name: str) -> tuple[Path, Path]:
    bin_path = _script(tmp_path / name / "ocr", "exit 0", fresh=False)
    src_path = tmp_path / name / "s.swift"
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text("// source")
    future = time.time() + 7200
    os.utime(src_path, (future, future))
    return bin_path, src_path


def test_parses_tsv_lines_zh_en(tmp_path: Path) -> None:
    out_file = tmp_path / "out.tsv"
    out_file.write_text(
        "\u4f60\u597d\u4e16\u754c\t10.0\t20.0\t100.0\t30.0\nHello\t200.5\t400.25\t80.0\t20.0\n"
    )
    binary = _script(tmp_path / "ocr", f'cat "{out_file}"')
    items = ocr_image("/tmp/x.png", binary=binary, home=meiva_home())  # noqa: S108
    assert items == [
        {"text": "\u4f60\u597d\u4e16\u754c", "x": 10.0, "y": 20.0, "w": 100.0, "h": 30.0},
        {"text": "Hello", "x": 200.5, "y": 400.25, "w": 80.0, "h": 20.0},
    ]


def test_skips_malformed_lines(tmp_path: Path) -> None:
    out_file = tmp_path / "out.tsv"
    out_file.write_text("short\t1.0\nbad\tx\ty\tw\th\nok\t1\t2\t3\t4\n")
    binary = _script(tmp_path / "ocr", f'cat "{out_file}"')
    items = ocr_image("/tmp/x.png", binary=binary, home=meiva_home())  # noqa: S108
    assert items == [{"text": "ok", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}]


def test_nonzero_exit_raises(tmp_path: Path) -> None:
    binary = _script(tmp_path / "ocr", 'echo "boom" >&2\nexit 1')
    with pytest.raises(OcrError, match="boom"):
        ocr_image("/tmp/x.png", binary=binary, home=meiva_home())  # noqa: S108


def test_timeout_raises(tmp_path: Path) -> None:
    binary = _script(tmp_path / "ocr", "sleep 5")
    with pytest.raises(OcrError, match="timed out"):
        ocr_image("/tmp/x.png", binary=binary, timeout=0.05, home=meiva_home())  # noqa: S108


def test_empty_output_is_empty_list(tmp_path: Path) -> None:
    binary = _script(tmp_path / "ocr", "exit 0")
    assert ocr_image("/tmp/x.png", binary=binary, home=meiva_home()) == []  # noqa: S108


def test_missing_swiftc_raises(tmp_path: Path) -> None:
    bin_path, src_path = _stale_pair(tmp_path, "rebuild1")
    with pytest.raises(OcrError, match="swiftc"):
        ocr_image("/tmp/x.png", binary=bin_path, source=src_path, swiftc="", home=meiva_home())  # noqa: S108


def test_swiftc_failure_raises(tmp_path: Path) -> None:
    bin_path, src_path = _stale_pair(tmp_path, "rebuild2")
    compiler = _script(tmp_path / "swiftc", 'echo "compile error" >&2\nexit 1')
    with pytest.raises(OcrError, match="compile error"):
        ocr_image(
            "/tmp/x.png",  # noqa: S108
            binary=bin_path,
            source=src_path,
            swiftc=str(compiler),
            home=meiva_home(),
        )


def test_existing_fresh_binary_skips_compile(tmp_path: Path) -> None:
    calls_file = tmp_path / "calls.txt"
    binary = _script(
        tmp_path / "ocr",
        f'printf "ocr:%s\\n" "$1" >> "{calls_file}"\nprintf "ok\\t1\\t2\\t3\\t4\\n"',
    )
    compiler = _script(
        tmp_path / "swiftc",
        f'printf "swiftc\\n" >> "{calls_file}"\nexit 1',
    )
    items = ocr_image("/tmp/x.png", binary=binary, swiftc=str(compiler), home=meiva_home())  # noqa: S108
    assert items == [{"text": "ok", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}]
    assert calls_file.read_text().splitlines() == ["ocr:/tmp/x.png"]
