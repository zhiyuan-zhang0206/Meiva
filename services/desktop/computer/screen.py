"""Screen capture and physical<->logical coordinate space for the computer-mcp
daemon.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from base.host.permissions_helper.wire import AxActResult, AxTreeResult
from base.paths import logs_dir

from .errors import ComputerUseError
from .targets import CaptureRegion


class ScaleHelper(Protocol):
    def screen_size(self) -> Mapping[str, Any]: ...


class ScreenHelper(ScaleHelper, Protocol):
    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str) -> object: ...


class DesktopHelper(ScreenHelper, Protocol):
    def ping(self) -> Mapping[str, Any]: ...
    def frontmost_app(self) -> Mapping[str, Any]: ...
    def cursor_position(self) -> Mapping[str, Any]: ...
    def click(
        self,
        x: float,
        y: float,
        *,
        double: bool = False,
        button: str = "left",
        click_count: int | None = None,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> Mapping[str, Any]: ...
    def move(
        self, x: float, y: float, *, modifiers: list[str] | None = None
    ) -> Mapping[str, Any]: ...
    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        button: str | None = None,
        modifiers: list[str] | None = None,
    ) -> Mapping[str, Any]: ...
    def mouse_down(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> Mapping[str, Any]: ...
    def mouse_up(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
    ) -> Mapping[str, Any]: ...
    def mouse_release_all(self) -> Mapping[str, Any]: ...
    def type_text(self, text: str) -> Mapping[str, Any]: ...
    def key(
        self,
        code: int,
        *,
        cmd: bool = False,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> Mapping[str, Any]: ...
    def scroll(
        self,
        x: float,
        y: float,
        dy: int,
        *,
        dx: int | None = None,
        modifiers: list[str] | None = None,
    ) -> Mapping[str, Any]: ...
    def clipboard_read(self) -> Mapping[str, Any]: ...
    def clipboard_write(self, text: str) -> Mapping[str, Any]: ...
    def indicator_stop(self) -> Mapping[str, Any]: ...
    def list_apps(self) -> Mapping[str, Any]: ...
    def list_windows(self, app: str | None = None) -> Mapping[str, Any]: ...
    def focus_app(self, target: dict[str, object]) -> Mapping[str, Any]: ...
    def screencapture_window(self, pid: int, window_id: int, path: str) -> Mapping[str, Any]: ...
    def ax_window_info(self, app: str) -> Mapping[str, Any]: ...
    def window_info(self, owner: str) -> Mapping[str, Any]: ...
    def session_info(self) -> Mapping[str, Any]: ...
    def ax_tree(
        self,
        app: str | None = None,
        *,
        target: dict[str, object] | None = None,
        scope: int | None = None,
        scope_fp: str | None = None,
        max_nodes: int = 600,
        max_depth: int = 14,
        budget_ms: int = 1500,
        timeout_ms: int = 400,
        enable_ax: bool = True,
    ) -> AxTreeResult: ...
    def ax_act(
        self,
        app: str,
        raw_id: int,
        action: str,
        *,
        pid: int | None = None,
        value: str | None = None,
        native_action: str | None = None,
        text: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        selection_type: str | None = None,
        timeout_ms: int = 2000,
    ) -> AxActResult: ...


def png_size(path: Path) -> tuple[int, int]:
    """Physical pixel size of a PNG from its IHDR (signature + 4-byte length +
    'IHDR' + width + height, big-endian). No image library needed."""
    with Path(path).open("rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ComputerUseError(f"captured file {path} is not a PNG")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def to_logical(value: float, scale: float) -> float:
    """Physical-pixel coordinate -> helper's logical-point space (divide by the
    measured backing scale; scale 1 (1x display) is a no-op there)."""
    return value / scale if scale > 1 else value


def _pixel_scale(pixels_w: int, logical_w: float) -> float:
    """Measure the physical->logical scale from the captured PNG itself.

    The PNG is the ground truth of the click space (IHDR width = physical
    pixels of exactly the region the caller clicks); the helper's report is
    not trusted here — a process without an AppKit event loop can hold stale
    screen objects (2026-08-30: scale 2 on a 1x display, halving every click).
    """
    if pixels_w <= 0 or logical_w <= 0:
        raise ComputerUseError(f"cannot compute screen scale from {pixels_w}px over {logical_w}pt")
    return pixels_w / logical_w


def current_scale(helper: ScaleHelper, measured: float | None) -> float:
    """The physical->logical scale for coordinate conversion.

    Prefers the daemon's own measurement (last snapshot, ``_pixel_scale``);
    before any snapshot, falls back to the helper's live report.
    """
    if measured is not None:
        return measured
    return float(helper.screen_size()["scale"])


# meiva:note
# The file-name stamp of a capture taken at `moment`, in UTC at microsecond resolution. The retention
# sweep (`meiva logs retention`) recognizes a capture by this exact shape, and one tool call takes at
# most one capture, so one stamp names it.
def snapshot_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")


def snapshot_path(agent_id: int | None, *, stamp: str) -> Path:
    """A fresh capture path under $MEIVA_HOME/logs/computer/snapshots/."""
    directory = logs_dir() / "computer" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"agent-{agent_id or 0}-{stamp}.png"


def capture_screen(
    helper: ScreenHelper, agent_id: int, *, stamp: str
) -> tuple[Path, Mapping[str, Any], float, tuple[int, int]]:
    """One full-screen capture: (PNG path, helper screen report, measured
    scale, physical pixel size).

    The capture shared by snapshot and the OCR text tools — screencapture -R
    takes logical points and clips to the display; the PNG comes out at
    physical resolution (Retina 2x), reported via IHDR. Measure, don't trust:
    the PNG is the ground truth callers click against; the helper's reported
    scale can be stale (see _pixel_scale).
    """
    path = snapshot_path(agent_id, stamp=stamp)
    size = helper.screen_size()
    helper.screencapture_region(0, 0, int(size["w"]), int(size["h"]), str(path))
    pw, ph = png_size(path)
    return path, size, _pixel_scale(pw, size["w"]), (pw, ph)


def capture_region(
    helper: ScreenHelper,
    region: CaptureRegion,
    *,
    path: Path,
) -> tuple[Path, float, tuple[int, int]]:
    """Capture a main-display rectangle without changing whole-screen state.

    The helper's region capture can clip out-of-display rectangles silently.
    Reject those before capture so the requested origin and measured scale
    describe the pixels that actually came back. Coordinates are global
    logical points; the resulting PNG's coordinates start at the region origin.
    """
    screen = helper.screen_size()
    if not (
        screen["x"] <= region.x
        and screen["y"] <= region.y
        and region.x + region.w <= screen["x"] + screen["w"]
        and region.y + region.h <= screen["y"] + screen["h"]
    ):
        raise ComputerUseError("region must be wholly inside the main display")
    helper.screencapture_region(region.x, region.y, region.w, region.h, str(path))
    pw, ph = png_size(path)
    scale = _pixel_scale(pw, region.w)
    if abs(ph - region.h * scale) > 1:
        raise ComputerUseError("captured region dimensions do not match the requested rectangle")
    return path, scale, (pw, ph)
