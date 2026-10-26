# meiva:note
# The "where / what" string of a computer_action audit row, one pure function per tool family.

from __future__ import annotations

from collections.abc import Callable, Mapping

__all__ = ["audit_coords"]

_AuditArgs = Mapping[str, object]
_AuditResult = Mapping[str, object] | None


# meiva:note
# Audited as given, also for a call refused for a missing coordinate.
def _coords_point(args: _AuditArgs, result: _AuditResult) -> str | None:
    del result
    return f"{args['x']},{args.get('y')}" if "x" in args else None


def _coords_drag(args: _AuditArgs, result: _AuditResult) -> str | None:
    del result
    return f"{args.get('start_x')},{args.get('start_y')}->{args.get('end_x')},{args.get('end_y')}"


# meiva:note
# click_text resolves its own target via OCR: audit the center it clicked (physical pixels), not an
# argument coordinate.
def _coords_click_text(args: _AuditArgs, result: _AuditResult) -> str | None:
    del args
    return None if result is None else f"{result.get('x')},{result.get('y')}"


# meiva:note
# The element's center and the action - never the value written.
def _coords_ax_act(args: _AuditArgs, result: _AuditResult) -> str | None:
    del args
    if result is None:
        return None
    return (
        f"{result.get('x')},{result.get('y')},{result.get('native_action', result.get('action'))}"
    )


def _coords_mouse_button(args: _AuditArgs, result: _AuditResult) -> str | None:
    del result
    where = f"{args['x']},{args.get('y')}" if "x" in args else "pointer"
    return f"{where},{args.get('button', 'left')}"


def _coords_scroll(args: _AuditArgs, result: _AuditResult) -> str | None:
    del result
    return f"{args.get('x')},{args.get('y')},{args.get('dy')}"


def _coords_key(args: _AuditArgs, result: _AuditResult) -> str | None:
    del result
    return str(args.get("key") or args.get("keycode") or "")


# meiva:note
# The clipboard may hold a secret: only a length is ever recorded, never the text.
def _coords_clipboard_write(args: _AuditArgs, result: _AuditResult) -> str | None:
    del args
    return None if result is None else f"{result.get('written')} chars"


def _coords_clipboard_read(args: _AuditArgs, result: _AuditResult) -> str | None:
    del args
    if result is None:
        return None
    return f"{result['length']} chars" if result.get("text") is not None else "no text"


_AUDIT_COORDS: dict[str, Callable[[_AuditArgs, _AuditResult], str | None]] = {
    "click": _coords_point,
    "move": _coords_point,
    "drag": _coords_drag,
    "click_text": _coords_click_text,
    "ax_act": _coords_ax_act,
    "scroll": _coords_scroll,
    "mouse_down": _coords_mouse_button,
    "mouse_up": _coords_mouse_button,
    "key": _coords_key,
    "clipboard_write": _coords_clipboard_write,
    "clipboard_read": _coords_clipboard_read,
}


# meiva:note
# The compact "where / what" string of a computer_action audit row.
def audit_coords(tool: str, args: _AuditArgs, result: _AuditResult) -> str | None:
    describe = _AUDIT_COORDS.get(tool)
    return None if describe is None else describe(args, result)
