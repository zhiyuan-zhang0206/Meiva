from __future__ import annotations

from typing import Literal

TreeQualityKind = Literal["good", "sparse", "canvas"]

INTERACTIVE_TYPES: frozenset[str] = frozenset(
    {
        "ButtonControl",
        "CheckBoxControl",
        "RadioButtonControl",
        "ComboBoxControl",
        "EditControl",
        "SliderControl",
        "HyperlinkControl",
        "MenuItemControl",
        "MenuBarControl",
        "TabItemControl",
        "ListItemControl",
        "TreeItemControl",
        "DataItemControl",
        "SplitButtonControl",
        "SpinnerControl",
        "ThumbControl",
    }
)
TEXT_TYPES: frozenset[str] = frozenset(
    {
        "TextControl",
        "EditControl",
        "DocumentControl",
        "HeaderItemControl",
        "StatusBarControl",
    }
)
CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "WindowControl",
        "PaneControl",
        "GroupControl",
        "ToolBarControl",
        "TabControl",
        "ListControl",
        "TreeControl",
        "TableControl",
        "MenuControl",
        "CustomControl",
    }
)


def classify_tree_quality(
    *, window_width: int, window_height: int, element_count: int, interactive_count: int
) -> tuple[TreeQualityKind, str]:
    area = max(0, window_width) * max(0, window_height)
    non_root = max(0, element_count - 1)
    if area >= 150_000 and interactive_count <= 4:
        return (
            "canvas",
            "Large window with few UIA interactive nodes (e.g. 3D/DirectX viewport or custom canvas). "
            "Use screenshot + mouse_click/mouse_move_drag for viewport elements.",
        )
    if non_root <= 3:
        return ("sparse", "Sparse UIA tree; try mode='full' or fall back to screenshot.")
    return ("good", "UIA tree looks reliable; prefer uia_act(element_id=...) for controls.")
