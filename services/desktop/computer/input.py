"""Strict input contracts and their derived MCP schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

from .errors import ComputerUseError
from .targets import ObservationFrame

Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Modifier = Literal["shift", "ctrl", "alt", "cmd"]
MouseButton = Literal["left", "right", "middle"]
# Longest clipboard text either direction carries, in Unicode code points. The helper enforces
# the same bound on its side of the wire.
CLIPBOARD_MAX_CHARS = 1_000_000
WheelDelta = Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)]


class ModifiedInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    modifiers: list[Modifier] = Field(default_factory=list[Modifier], max_length=4)

    @model_validator(mode="after")
    def unique_modifiers(self) -> ModifiedInput:
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("modifiers must not contain duplicates")
        return self


class PositionedInput(ModifiedInput):
    frame: ObservationFrame | None = None


class ClickInput(PositionedInput):
    x: Coordinate
    y: Coordinate
    button: MouseButton = "left"
    click_count: int = Field(default=1, ge=1, le=3)
    double: bool = False
    duration_ms: Coordinate = Field(default=0, ge=0, le=5000)

    @model_validator(mode="after")
    def double_count_agree(self) -> ClickInput:
        if self.double and "click_count" in self.model_fields_set and self.click_count != 2:
            raise ValueError("double=true requires click_count=2 when both are supplied")
        return self


class KeyInput(ModifiedInput):
    key: str | None = Field(default=None, min_length=1)
    keycode: int | None = Field(default=None, ge=0, le=65535)
    cmd: bool = False
    duration_ms: Coordinate = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def one_key(self) -> KeyInput:
        if (self.key is None) == (self.keycode is None):
            raise ValueError("key requires exactly one key name or integer keycode")
        return self


def _check_optional_position(
    tool: str, x: float | None, y: float | None, frame: ObservationFrame | None
) -> None:
    """An optional pointer position is both coordinates or neither, and a frame needs them."""
    if (x is None) != (y is None):
        raise ValueError(f"{tool} requires both x and y when positioning explicitly")
    if frame is not None and x is None:
        raise ValueError(f"{tool} frame requires explicit x and y")


class ScrollInput(PositionedInput):
    x: Coordinate | None = None
    y: Coordinate | None = None
    dx: WheelDelta = 0
    dy: WheelDelta = 0

    @model_validator(mode="after")
    def paired_position_and_delta(self) -> ScrollInput:
        _check_optional_position("scroll", self.x, self.y, self.frame)
        if not self.model_fields_set.intersection({"dx", "dy"}):
            raise ValueError("scroll requires dx or dy")
        return self


class ClipboardWriteInput(BaseModel):
    """Plain text for the clipboard. Validation errors never echo the value: it may be a secret."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    text: str = Field(max_length=CLIPBOARD_MAX_CHARS)


class MoveInput(PositionedInput):
    x: Coordinate
    y: Coordinate


class DragInput(PositionedInput):
    start_x: Coordinate
    start_y: Coordinate
    end_x: Coordinate
    end_y: Coordinate
    button: MouseButton = "left"


class MouseDownInput(PositionedInput):
    """Press and keep a button down; `modifiers` are held until the matching mouse_up."""

    x: Coordinate | None = None
    y: Coordinate | None = None
    button: MouseButton = "left"

    @model_validator(mode="after")
    def paired_position(self) -> MouseDownInput:
        _check_optional_position("mouse_down", self.x, self.y, self.frame)
        return self


class MouseUpInput(BaseModel):
    """Release the held button. It takes no modifiers: those belong to the press."""

    model_config = ConfigDict(strict=True, extra="forbid")
    frame: ObservationFrame | None = None
    x: Coordinate | None = None
    y: Coordinate | None = None
    button: MouseButton = "left"

    @model_validator(mode="after")
    def paired_position(self) -> MouseUpInput:
        _check_optional_position("mouse_up", self.x, self.y, self.frame)
        return self


def validate_input[InputModel: BaseModel](
    model: type[InputModel], args: dict[str, object]
) -> InputModel:
    """Validate action parameters while preserving the daemon's coordination envelope."""
    action = {key: value for key, value in args.items() if key not in {"task_id", "priority"}}
    try:
        return model.model_validate(action)
    except ValidationError as exc:
        raise ComputerUseError(str(exc)) from exc


def tool_schema(model: type[BaseModel]) -> dict[str, object]:
    """Expose the same input contract to MCP, plus screen coordination arguments."""
    schema = model.model_json_schema()
    properties = object_of(schema.get, "properties")
    if not is_object_dict(properties):
        raise TypeError("a tool input model declares its properties")
    properties.update(
        {
            "task_id": {"type": "integer"},
            "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
        }
    )
    return schema


# meiva:note
# macOS virtual keycodes for the key tool's name/character vocabulary. LLM callers cannot be expected
# to know raw keycodes, so the MCP surface takes names ('return', 'space', ...) or single characters;
# the raw keycode stays available via the optional `keycode` argument (the helper API is code-based).
KEYCODES: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "`": 50,
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "backspace": 51,
    "delete": 51,
    "escape": 53,
    "cmd": 55,
    "shift": 56,
    "alt": 58,
    "ctrl": 59,
    "esc": 53,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    "forwarddelete": 117,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}


# meiva:note
# Virtual keycode for a key name or single character (case-insensitive).
def keycode_for(key: str) -> int | None:
    return KEYCODES.get(key.lower())
