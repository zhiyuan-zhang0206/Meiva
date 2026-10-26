"""Permissions-helper calls whose effect outlives the call: the clipboard and a held mouse button.

Both change state the user can see after the request returns, so each refuses a helper that
does not advertise it (`require_capability`) and neither puts the content it carries into an
error: the clipboard text may be a secret, and a held button must be traceable but not narrated.
"""

from __future__ import annotations

from typing import Literal, overload

from .wire import (
    AliveResult,
    AppsResult,
    AxActResult,
    AxTreeResult,
    AxWindowInfo,
    ClickPoint,
    ClickResult,
    ClipboardReadResult,
    ClipboardWriteResult,
    DragResult,
    FileListResult,
    FileReadResult,
    FocusAppResult,
    FrontmostApp,
    HelperShutdownResult,
    IndicatorStopResult,
    KeyResult,
    MouseDownResult,
    MouseReleaseAllResult,
    MouseUpResult,
    MoveResult,
    PingResult,
    RequestGrantsResult,
    RootStatus,
    ScreencaptureResult,
    ScreenSize,
    ScrollResult,
    SessionInfo,
    SessionListResult,
    SignalResult,
    SpawnResult,
    TypeResult,
    WindowCaptureResult,
    WindowInfo,
    WindowsResult,
)


class StatefulCalls:
    """The helper calls whose effect outlives the call; `PermissionsHelperClient` inherits them.

    They sit apart only to keep the client module within the file budget: each one rides the
    client's own `call` and `require_capability`."""

    def _call_raw(self, method: str, **args: object) -> object:
        raise NotImplementedError

    @overload
    def call(self, method: Literal["ping"], **args: object) -> PingResult: ...

    @overload
    def call(
        self, method: Literal["screencapture_region"], **args: object
    ) -> ScreencaptureResult: ...

    @overload
    def call(self, method: Literal["file_list"], **args: object) -> FileListResult: ...

    @overload
    def call(self, method: Literal["file_read"], **args: object) -> FileReadResult: ...

    @overload
    def call(self, method: Literal["click"], **args: object) -> ClickResult: ...

    @overload
    def call(self, method: Literal["move"], **args: object) -> MoveResult: ...

    @overload
    def call(self, method: Literal["cursor_position"], **args: object) -> ClickPoint: ...

    @overload
    def call(self, method: Literal["drag"], **args: object) -> DragResult: ...

    @overload
    def call(self, method: Literal["type"], **args: object) -> TypeResult: ...

    @overload
    def call(self, method: Literal["key"], **args: object) -> KeyResult: ...

    @overload
    def call(self, method: Literal["scroll"], **args: object) -> ScrollResult: ...

    @overload
    def call(self, method: Literal["indicator_stop"], **args: object) -> IndicatorStopResult: ...

    @overload
    def call(self, method: Literal["list_apps"], **args: object) -> AppsResult: ...

    @overload
    def call(self, method: Literal["list_windows"], **args: object) -> WindowsResult: ...

    @overload
    def call(self, method: Literal["focus_app"], **args: object) -> FocusAppResult: ...

    @overload
    def call(
        self, method: Literal["screencapture_window"], **args: object
    ) -> WindowCaptureResult: ...

    @overload
    def call(self, method: Literal["ax_window_info"], **args: object) -> AxWindowInfo: ...

    @overload
    def call(self, method: Literal["ax_tree"], **args: object) -> AxTreeResult: ...

    @overload
    def call(self, method: Literal["ax_act"], **args: object) -> AxActResult: ...

    @overload
    def call(self, method: Literal["window_info"], **args: object) -> WindowInfo: ...

    @overload
    def call(self, method: Literal["session_info"], **args: object) -> SessionInfo: ...

    @overload
    def call(self, method: Literal["spawn"], **args: object) -> SpawnResult: ...

    @overload
    def call(self, method: Literal["session_list"], **args: object) -> SessionListResult: ...

    @overload
    def call(self, method: Literal["session_has"], **args: object) -> AliveResult: ...

    @overload
    def call(self, method: Literal["signal"], **args: object) -> SignalResult: ...

    @overload
    def call(
        self, method: Literal["root_seed", "root_status", "root_stop"], **args: object
    ) -> RootStatus: ...

    @overload
    def call(self, method: Literal["helper_shutdown"], **args: object) -> HelperShutdownResult: ...

    @overload
    def call(self, method: Literal["screen_size"], **args: object) -> ScreenSize: ...

    @overload
    def call(self, method: Literal["frontmost_app"], **args: object) -> FrontmostApp: ...

    @overload
    def call(self, method: Literal["request_grants"], **args: object) -> RequestGrantsResult: ...

    @overload
    def call(self, method: Literal["mouse_down"], **args: object) -> MouseDownResult: ...

    @overload
    def call(self, method: Literal["mouse_up"], **args: object) -> MouseUpResult: ...

    @overload
    def call(
        self, method: Literal["mouse_release_all"], **args: object
    ) -> MouseReleaseAllResult: ...

    @overload
    def call(self, method: Literal["clipboard_read"], **args: object) -> ClipboardReadResult: ...

    @overload
    def call(self, method: Literal["clipboard_write"], **args: object) -> ClipboardWriteResult: ...

    def call(self, method: str, **args: object) -> object:
        return self._call_raw(method, **args)

    def require_capability(self, capability: str, use: str) -> None:
        raise NotImplementedError

    def mouse_down(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> MouseDownResult:
        """Press a mouse button and keep it down across calls, at a logical global point (the
        pointer's own position when none is given). `modifiers` come down first and stay down until
        the release. At most one button is held; the helper lets it go by itself on a stop gesture,
        after 30 idle seconds, or on `mouse_release_all`."""
        self.require_capability(
            "mouse_hold_v1",
            "mouse_down/mouse_up",
        )
        args: dict[str, object] = {"button": button}
        if x is not None and y is not None:
            args.update(x=x, y=y)
        elif x is not None or y is not None:
            raise ValueError("mouse_down takes both x and y, or neither")
        if modifiers is not None:
            args["modifiers"] = modifiers
        return self.call("mouse_down", **args)

    def mouse_up(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
    ) -> MouseUpResult:
        """Release the button `mouse_down` pressed, at a logical global point (dragging there first)
        or where the pointer is. Refused with an error when that button is not held."""
        self.require_capability(
            "mouse_hold_v1",
            "mouse_down/mouse_up",
        )
        args: dict[str, object] = {"button": button}
        if x is not None and y is not None:
            args.update(x=x, y=y)
        elif x is not None or y is not None:
            raise ValueError("mouse_up takes both x and y, or neither")
        return self.call("mouse_up", **args)

    def mouse_release_all(self) -> MouseReleaseAllResult:
        """Release whatever button Meiva holds, quietly and regardless of a stop gesture: the cleanup
        the daemon runs when the screen changes hands. Names the buttons it released."""
        self.require_capability(
            "mouse_hold_v1",
            "mouse_down/mouse_up",
        )
        return self.call(
            "mouse_release_all",
        )

    def clipboard_read(self) -> ClipboardReadResult:
        """Read the clipboard's plain text and the list of types it currently holds.

        `text` is None when no plain text is on the clipboard. The content is returned to the
        caller only; nothing in this layer logs it."""
        self.require_capability(
            "clipboard_v1",
            "the clipboard",
        )
        return self.call(
            "clipboard_read",
        )

    def clipboard_write(self, text: str) -> ClipboardWriteResult:
        """Replace the clipboard with plain text (`public.utf8-plain-text`) and nothing else.

        The reply carries the length only, so the text is never echoed into a log or an audit row."""
        self.require_capability(
            "clipboard_v1",
            "the clipboard",
        )
        return self.call(
            "clipboard_write",
            text=text,
        )
