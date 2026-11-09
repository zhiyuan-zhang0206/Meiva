"""Cropped captures retain their geometry and never hide system clipping."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from base.host.permissions_helper.wire import ScreenSize

from .. import screen
from ..errors import ComputerUseError
from ..targets import CaptureRegion


class _Display:
    """A main display at (-400,-100) 800x600; a capture writes a PNG of `png` pixels."""

    def __init__(self) -> None:
        self.png: tuple[int, int] | None = (400, 200)
        self.captured: list[tuple[int, int, int, int]] = []

    def screen_size(self) -> ScreenSize:
        return {"x": -400, "y": -100, "w": 800, "h": 600, "scale": 99}

    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str) -> None:
        if self.png is None:
            pytest.fail("out-of-display capture must not run")
        self.captured.append((x, y, w, h))
        _png(path, *self.png)


@pytest.fixture
def helper() -> _Display:
    return _Display()


def _png(path: str, width: int, height: int) -> None:
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)
    )


def test_capture_preserves_origin_and_measures_retina_scale(
    helper: _Display, tmp_path: Path
) -> None:
    path = tmp_path / "capture.png"
    assert screen.capture_region(helper, CaptureRegion(-350, -50, 200, 100), path=path) == (
        path,
        2,
        (400, 200),
    )
    assert helper.captured == [(-350, -50, 200, 100)]


@pytest.mark.parametrize(
    "region",
    [
        CaptureRegion(-401, 0, 10, 10),
        CaptureRegion(0, -101, 10, 10),
        CaptureRegion(395, 0, 10, 10),
        CaptureRegion(0, 495, 10, 10),
    ],
)
def test_outside_main_display_fails_before_capture(
    helper: _Display, region: CaptureRegion, tmp_path: Path
) -> None:
    helper.png = None
    with pytest.raises(ComputerUseError, match="main display"):
        screen.capture_region(helper, region, path=tmp_path / "capture.png")


def test_unexpected_clipping_is_an_error(helper: _Display, tmp_path: Path) -> None:
    helper.png = (400, 170)
    with pytest.raises(ComputerUseError, match="dimensions"):
        screen.capture_region(helper, CaptureRegion(0, 0, 200, 100), path=tmp_path / "capture.png")


class _ScaleHelper:
    def __init__(self, size: ScreenSize) -> None:
        self._size = size

    def screen_size(self) -> ScreenSize:
        return self._size


@pytest.mark.parametrize(
    ("size", "mode", "expected"),
    [
        ({"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0, "scale": 1.0}, (1920, 1920), 1.0),
        ({"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0, "scale": 1.0}, (1920, 3840), 2.0),
        ({"x": 0.0, "y": 0.0, "w": 1710.0, "h": 1107.0, "scale": 1.0}, (1710, 3420), 2.0),
        ({"x": 0.0, "y": 0.0, "w": 1710.0, "h": 1107.0, "scale": 2.0}, None, 2.0),
        ({"x": 0.0, "y": 0.0, "w": 1710.0, "h": 1107.0, "scale": 1.0}, None, 1.0),
        ({"x": 0.0, "y": 0.0, "w": 1710.0, "h": 1107.0, "scale": 1.0}, (1920, 3840), 1.0),
    ],
)
def test_cold_start_current_scale_reconciles_display_modes(
    size: ScreenSize, mode: tuple[int, int] | None, expected: float
) -> None:
    sh = _ScaleHelper(size)
    assert screen.current_scale(sh, None, display_mode=lambda: mode) == expected
    assert screen.current_scale(sh, 1.0, display_mode=lambda: mode) == 1.0
    assert screen.current_scale(sh, 2.0, display_mode=lambda: mode) == 2.0


def test_measured_tool_scale_covers_cold_start_and_preserves_warm_region() -> None:
    seeded = screen.measured_tool_scale(
        "snapshot",
        {"region": {"x": 10, "y": 20, "w": 100, "h": 50}},
        {"source": "region", "frame": {"scale": 2.0}},
        None,
    )
    assert seeded == 2.0
    preserved = screen.measured_tool_scale(
        "snapshot",
        {"region": {"x": 10, "y": 20, "w": 100, "h": 50}},
        {"source": "region", "frame": {"scale": 2.0}},
        1.0,
    )
    assert preserved == 1.0
    assert (
        screen.measured_tool_scale("find_text", {"text": "ok"}, {"fresh": True}, None, 2.0, None)
        == 2.0
    )
    assert (
        screen.measured_tool_scale(
            "ax_tree",
            {"include_ocr_gap": True},
            {"visual_only": 1, "tree": "[e1] window"},
            None,
            None,
            2.0,
        )
        == 2.0
    )
