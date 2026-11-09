"""Screen capture and physical<->logical coordinate space for the computer-mcp
daemon.
"""

from __future__ import annotations

import ctypes
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict

from base.host.boundary.fields import optional_float_field
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.permissions_helper.wire import (
    AppsResult,
    AxActResult,
    AxTreeResult,
    AxWindowInfo,
    ClickPoint,
    ClickResult,
    ClipboardReadResult,
    ClipboardWriteResult,
    DragResult,
    FocusAppResult,
    FrontmostApp,
    IndicatorStopResult,
    KeyResult,
    MouseDownResult,
    MouseReleaseAllResult,
    MouseUpResult,
    MoveResult,
    PingResult,
    ScreenSize,
    ScrollResult,
    SessionInfo,
    TypeResult,
    WindowCaptureResult,
    WindowInfo,
    WindowsResult,
)
from base.paths import logs_dir

from .errors import ComputerUseError
from .targets import CaptureRegion


# meiva:note
# The optional parameters a helper call takes, as the caller set them: a field the caller left unset
# stays out, so the helper client does not demand a capability for a default it never needed.
class ClickOptions(TypedDict, total=False):
    double: bool
    button: str
    click_count: int
    modifiers: list[str]
    duration_ms: float


class KeyOptions(TypedDict, total=False):
    cmd: bool
    modifiers: list[str]
    duration_ms: float


class ButtonOptions(TypedDict, total=False):
    button: str
    modifiers: list[str]


class MouseUpOptions(TypedDict, total=False):
    button: str


class ScrollOptions(TypedDict, total=False):
    dx: int
    modifiers: list[str]


class ScaleHelper(Protocol):
    def screen_size(self) -> ScreenSize: ...


class ScreenHelper(ScaleHelper, Protocol):
    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str) -> object: ...


class DesktopHelper(ScreenHelper, Protocol):
    def ping(self) -> PingResult: ...
    def frontmost_app(self) -> FrontmostApp: ...
    def cursor_position(self) -> ClickPoint: ...
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
    ) -> ClickResult: ...
    def move(self, x: float, y: float, *, modifiers: list[str] | None = None) -> MoveResult: ...
    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        button: str | None = None,
        modifiers: list[str] | None = None,
    ) -> DragResult: ...
    def mouse_down(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> MouseDownResult: ...
    def mouse_up(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
    ) -> MouseUpResult: ...
    def mouse_release_all(self) -> MouseReleaseAllResult: ...
    def type_text(self, text: str) -> TypeResult: ...
    def key(
        self,
        code: int,
        *,
        cmd: bool = False,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> KeyResult: ...
    def scroll(
        self,
        x: float,
        y: float,
        dy: int,
        *,
        dx: int | None = None,
        modifiers: list[str] | None = None,
    ) -> ScrollResult: ...
    def clipboard_read(self) -> ClipboardReadResult: ...
    def clipboard_write(self, text: str) -> ClipboardWriteResult: ...
    def indicator_stop(self) -> IndicatorStopResult: ...
    def list_apps(self) -> AppsResult: ...
    def list_windows(self, app: str | None = None) -> WindowsResult: ...
    def focus_app(self, target: dict[str, object]) -> FocusAppResult: ...
    def screencapture_window(self, pid: int, window_id: int, path: str) -> WindowCaptureResult: ...
    def ax_window_info(self, app: str) -> AxWindowInfo: ...
    def window_info(self, owner: str) -> WindowInfo: ...
    def session_info(self) -> SessionInfo: ...
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


# meiva:note
# Logical width and physical pixel width of the active main display mode from CoreGraphics
# (`CGDisplayCopyDisplayMode`), or `None` when unavailable.
def main_display_mode_dimensions() -> tuple[int, int] | None:
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
        cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
        cg.CGDisplayModeGetWidth.argtypes = [ctypes.c_void_p]
        cg.CGDisplayModeGetWidth.restype = ctypes.c_size_t
        cg.CGDisplayModeGetPixelWidth.argtypes = [ctypes.c_void_p]
        cg.CGDisplayModeGetPixelWidth.restype = ctypes.c_size_t
        cg.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]
        cg.CGDisplayModeRelease.restype = None
        display_id = object_of(cg.CGMainDisplayID)
        if not isinstance(display_id, int):
            return None
        mode = object_of(cg.CGDisplayCopyDisplayMode, display_id)
        if not isinstance(mode, int) or mode == 0:
            return None
        try:
            width = object_of(cg.CGDisplayModeGetWidth, mode)
            pixel_width = object_of(cg.CGDisplayModeGetPixelWidth, mode)
            if not isinstance(width, int) or not isinstance(pixel_width, int):
                return None
            return width, pixel_width
        finally:
            _ = object_of(cg.CGDisplayModeRelease, mode)
    except (OSError, AttributeError):
        return None


# meiva:note
# Backing scale from a helper `screen_size` report plus the active CoreGraphics display mode
# `(width_pt, pixel_width)`. Older permissions-helper builds compute `scale` from
# `CGDisplayPixelsWide` over `CGDisplayBounds.width`, both of which CoreGraphics reports in logical
# points on HiDPI/Retina modes (`scale=1` on a `2x` display). When `size["scale"] <= 1` and the
# active display mode's logical width matches `size["w"]`, `pixel_width / width_pt` supplies the
# backing scale.
def resolve_screen_scale(
    size: ScreenSize,
    *,
    display_mode: Callable[[], tuple[int, int] | None] = main_display_mode_dimensions,
) -> float:
    reported = float(size["scale"])
    logical_w = float(size["w"])
    if reported > 1 or logical_w <= 0:
        return reported
    mode = display_mode()
    if mode is None:
        return reported
    mode_w, pixel_w = mode
    if mode_w <= 0 or pixel_w <= mode_w or abs(mode_w - logical_w) > 1:
        return reported
    return pixel_w / mode_w


# meiva:note
# The physical->logical scale for coordinate conversion: prefers the daemon's own measurement
# (`_pixel_scale`); before any snapshot, falls back to the helper's live report reconciled with the
# active CoreGraphics display mode when `scale <= 1`.
def current_scale(
    helper: ScaleHelper,
    measured: float | None,
    *,
    display_mode: Callable[[], tuple[int, int] | None] = main_display_mode_dimensions,
) -> float:
    if measured is not None:
        return measured
    return resolve_screen_scale(helper.screen_size(), display_mode=display_mode)


def _required_float(record: dict[str, object], key: str) -> float:
    value = optional_float_field(record, key)
    if value is None:
        raise TypeError(f"{key} is not a number")
    return value


def _snapshot_scale(result: dict[str, object], current: float | None) -> float | None:
    if "frame" in result:
        return current
    screen = result["screen"]
    if not is_object_dict(screen):
        raise TypeError("the snapshot result carries no screen")
    return _required_float(screen, "scale")


# meiva:note
# Updated whole-screen physical->logical scale after `tool` completes with `result`. Every
# whole-screen `capture_screen` producer (`snapshot` without `frame`, `click_text`, fresh
# `find_text`, and `ax_tree` with `include_ocr_gap`) establishes the latest measured scale; local
# frame captures (`region` / `window`) never replace or seed the global scale.
def measured_tool_scale(
    tool: str,
    args: dict[str, object],
    result: dict[str, object],
    current: float | None,
    ocr_scale: float | None = None,
    visual_scale: float | None = None,
) -> float | None:
    if tool == "snapshot":
        return _snapshot_scale(result, current)
    if tool == "click_text":
        return _required_float(result, "scale")
    if tool == "find_text":
        return ocr_scale if result.get("fresh") is True and ocr_scale is not None else current
    if tool == "ax_tree" and bool(args.get("include_ocr_gap")) and "visual_only" in result:
        return visual_scale if visual_scale is not None else current
    return current


# meiva:note
# The file-name stamp of a capture taken at `moment`, in UTC at microsecond resolution. The retention
# sweep (`meiva logs retention`) recognizes a capture by this exact shape, and one tool call takes at
# most one capture, so one stamp names it.
def snapshot_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")


def snapshot_path(agent_id: int | None, *, stamp: str, home: Path) -> Path:
    """A fresh capture path under $MEIVA_HOME/logs/computer/snapshots/."""
    directory = logs_dir(home=home) / "computer" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"agent-{agent_id or 0}-{stamp}.png"


def capture_screen(
    helper: ScreenHelper, agent_id: int, *, stamp: str, home: Path
) -> tuple[Path, ScreenSize, float, tuple[int, int]]:
    """One full-screen capture: (PNG path, helper screen report, measured
    scale, physical pixel size).

    The capture shared by snapshot and the OCR text tools — screencapture -R
    takes logical points and clips to the display; the PNG comes out at
    physical resolution (Retina 2x), reported via IHDR. Measure, don't trust:
    the PNG is the ground truth callers click against; the helper's reported
    scale can be stale (see _pixel_scale).
    """
    path = snapshot_path(agent_id, stamp=stamp, home=home)
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
