# ruff: noqa: N802
from __future__ import annotations

from collections.abc import Sequence

class _Rect:
    left: int
    top: int
    right: int
    bottom: int

class _PatternId:
    InvokePattern: int
    ValuePattern: int
    TogglePattern: int
    SelectionItemPattern: int
    ExpandCollapsePattern: int
    ScrollPattern: int

PatternId: _PatternId

class _Pattern:
    Value: str
    ToggleState: int
    def Invoke(self) -> None: ...
    def SetValue(self, value: str) -> None: ...
    def Toggle(self) -> None: ...
    def Select(self) -> None: ...
    def Expand(self) -> None: ...
    def Collapse(self) -> None: ...

class Control:
    ControlTypeName: str
    Name: str
    AutomationId: str
    IsOffscreen: bool
    BoundingRectangle: _Rect | None
    def GetPattern(self, pattern_id: int, /) -> _Pattern | None: ...
    def GetChildren(self) -> Sequence[Control]: ...
    def SetFocus(self) -> None: ...

class UIAutomationInitializerInThread:
    def __init__(self) -> None: ...

def SetGlobalSearchTimeout(timeout: float) -> None: ...
def ControlFromHandle(handle: int) -> Control | None: ...
