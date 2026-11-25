from __future__ import annotations

import re

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def close_open_fence(body: str) -> str:
    opened: str | None = None
    for line in body.splitlines():
        match = _FENCE.match(line)
        if match is None:
            continue
        run, rest = match[1], match[2]
        if opened is None:
            if run[0] == "`" and "`" in rest:
                continue
            opened = run
        elif run[0] == opened[0] and len(run) >= len(opened) and not rest.strip():
            opened = None
    if opened is None:
        return body
    return f"{body}\n{opened}"
