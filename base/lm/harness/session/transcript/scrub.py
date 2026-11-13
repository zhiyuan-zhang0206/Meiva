from __future__ import annotations

import re

from base.host.boundary.json_values import is_object_dict, is_object_list

_DIRTY = re.compile("[\x00\ud800-\udfff]")


# meiva:note
# Make a decoded JSON value storable as `jsonb`, in place: the NUL character is removed and a lone surrogate
# becomes U+FFFD, in every string and key. Returns the value, or the cleaned string when given a string. The
# window read runs it on every raw row before the row is parsed.
def scrub(value: object) -> object:
    if isinstance(value, str):
        return _clean(value)
    pending: list[object] = [value]
    while pending:
        node = pending.pop()
        if is_object_dict(node):
            _scrub_dict(node, pending)
        elif is_object_list(node):
            _scrub_list(node, pending)
    return value


def _scrub_dict(node: dict[str, object], pending: list[object]) -> None:
    for key in list(node):
        item = node[key]
        if isinstance(item, str):
            node[key] = _clean(item)
        elif is_object_dict(item) or is_object_list(item):
            pending.append(item)
        if _DIRTY.search(key):
            node[_clean(key)] = node.pop(key)


def _scrub_list(node: list[object], pending: list[object]) -> None:
    for index, item in enumerate(node):
        if isinstance(item, str):
            node[index] = _clean(item)
        elif is_object_dict(item) or is_object_list(item):
            pending.append(item)


def _clean(text: str) -> str:
    if _DIRTY.search(text) is None:
        return text
    return _DIRTY.sub(lambda found: "" if found.group() == "\x00" else "�", text)
