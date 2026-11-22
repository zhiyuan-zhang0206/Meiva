from __future__ import annotations

from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.permissions_helper.wire import AxNode, AxTreeResult

_UIA_ROLE_TO_AX: dict[str, str] = {
    "Window": "AXWindow",
    "Button": "AXButton",
    "SplitButton": "AXMenuButton",
    "CheckBox": "AXCheckBox",
    "RadioButton": "AXRadioButton",
    "ComboBox": "AXComboBox",
    "Edit": "AXTextField",
    "Document": "AXTextArea",
    "Slider": "AXSlider",
    "Hyperlink": "AXLink",
    "MenuItem": "AXMenuItem",
    "MenuBar": "AXMenuBarItem",
    "TabItem": "AXTab",
    "Tab": "AXTabGroup",
    "ListItem": "AXRow",
    "TreeItem": "AXRow",
    "DataItem": "AXRow",
    "Spinner": "AXIncrementor",
    "Text": "AXStaticText",
    "HeaderItem": "AXHeading",
    "StatusBar": "AXStaticText",
    "Pane": "AXGroup",
    "Group": "AXGroup",
    "ToolBar": "AXToolbar",
    "List": "AXList",
    "Tree": "AXOutline",
    "Table": "AXTable",
    "Menu": "AXMenu",
    "Image": "AXImage",
    "Custom": "AXGroup",
}

_PATTERN_TO_AX_ACTION: dict[str, str] = {
    "Invoke": "AXPress",
    "Toggle": "AXPress",
    "Select": "AXPress",
    "Expand": "AXPress",
}


def _numeric_rect(raw: object) -> tuple[float, float, float, float] | None:
    if not is_object_list(raw) or len(raw) != 4:
        return None
    nums: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        nums.append(float(item))
    return nums[0], nums[1], nums[2], nums[3]


def _win_element_id_to_int(raw_id: object, fallback: int) -> tuple[int, str]:
    if isinstance(raw_id, str) and raw_id.startswith("e") and raw_id[1:].isdigit():
        return int(raw_id[1:]), raw_id
    return fallback, f"e{fallback}"


def _extract_ax_actions(raw_patterns: object) -> list[str]:
    actions: list[str] = []
    if not is_object_list(raw_patterns):
        return actions
    for pat in raw_patterns:
        if isinstance(pat, str) and pat in _PATTERN_TO_AX_ACTION:
            act_name = _PATTERN_TO_AX_ACTION[pat]
            if act_name not in actions:
                actions.append(act_name)
    return actions


def _populate_optional_node_fields(node: AxNode, item: dict[str, object]) -> None:
    name = str(item.get("name") or "")
    auto_id = str(item.get("automation_id") or "")
    if name:
        node["title"] = name
    if auto_id:
        node["ident"] = auto_id
    val = item.get("value")
    if isinstance(val, str) and val:
        node["value"] = val
    rect = _numeric_rect(item.get("rect"))
    if rect is not None:
        node["x"], node["y"], node["w"], node["h"] = rect
    actions = _extract_ax_actions(item.get("patterns"))
    if actions:
        node["actions"] = actions


def _build_ax_node(
    item: dict[str, object], *, raw_num: int, root_id: int | None, total_elements: int
) -> AxNode:
    short_role = str(item.get("role") or "Unknown")
    ax_role = _UIA_ROLE_TO_AX.get(short_role, f"AX{short_role}")
    label = str(item.get("automation_id") or item.get("name") or raw_num)
    node: AxNode = {
        "id": raw_num,
        "fp": f"{ax_role}:{label}",
        "depth": 0 if root_id is None else 1,
        "n": max(0, total_elements - 1) if root_id is None else 0,
        "role": ax_role,
    }
    if root_id is not None:
        node["parent"] = root_id
    _populate_optional_node_fields(node, item)
    return node


def _safe_int(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def win_uia_to_ax_tree(
    win_result: Mapping[str, object],
) -> tuple[AxTreeResult, dict[int, str]]:
    win_obj = win_result.get("window")
    win = win_obj if is_object_dict(win_obj) else {}
    app_name = str(win.get("process") or win.get("title") or "Windows")
    raw_elements = win_result.get("elements")
    elements = raw_elements if is_object_list(raw_elements) else []

    nodes: list[AxNode] = []
    id_map: dict[int, str] = {}
    root_id: int | None = None
    for idx, item in enumerate(elements, start=1):
        if not is_object_dict(item):
            continue
        raw_num, win_eid = _win_element_id_to_int(item.get("id"), idx)
        id_map[raw_num] = win_eid
        nodes.append(
            _build_ax_node(item, raw_num=raw_num, root_id=root_id, total_elements=len(elements))
        )
        if root_id is None:
            root_id = raw_num

    ax_result: AxTreeResult = {
        "app": app_name,
        "pid": _safe_int(win.get("pid")),
        "windows": 1 if win else 0,
        "framework": "",
        "nodes": nodes,
        "visited": len(nodes),
        "truncated": bool(win_result.get("truncated", False)),
        "timed_out": False,
        "unreadable": 0,
        "elapsed_ms": _safe_int(win_result.get("elapsed_ms", 0)),
    }
    return ax_result, id_map
