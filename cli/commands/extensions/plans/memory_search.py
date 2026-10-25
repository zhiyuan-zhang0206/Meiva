# meiva:note
# `meiva memory search`: the request the gateway is asked and the text its answer becomes.
#
# Values in, text out. The shell dials the gateway and prints; this module decides the request
# body and the rendering, and refuses an answer that is not the documented shape.

from __future__ import annotations

import json
from typing import TypeGuard

__all__ = ["render_results", "search_body"]


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The JSON body of `POST /api/memory/search`: `limit` travels as `k`, and the purpose names the
# caller so the gateway can tell a person's search from an agent's recall.
def search_body(query: str, limit: int) -> dict[str, str | int]:
    return {"query": query, "k": limit, "purpose": "search"}


# meiva:note
# Nullable display metadata becomes empty text for the table; the JSON rendering keeps the
# answer untouched, nulls included.
def _rows(results: list[object]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for result in results:
        if not _is_object_dict(result):
            raise TypeError("memory search results must contain objects")
        raw_tags = result.get("tags")
        tags = (
            ", ".join(str(tag) for tag in raw_tags)
            if _is_object_list(raw_tags) and raw_tags
            else ""
        )
        raw_description = result.get("description")
        description = "" if raw_description is None else str(raw_description)
        score = result["score"]
        if not isinstance(score, int | float):
            raise TypeError("memory search result score must be numeric")
        rows.append((f"{score:.2f}", str(result["path"]), tags, description))
    return rows


def _table(results: list[object]) -> str:
    rows = _rows(results)
    path_width = max([len("path"), *(len(path) for _, path, _, _ in rows)])
    tags_width = max([len("tags"), *(len(tags) for _, _, tags, _ in rows)])
    lines = [f"score  {'path'.ljust(path_width)}  {'tags'.ljust(tags_width)}  description"]
    lines.extend(
        f"{score.ljust(5)}  {path.ljust(path_width)}  {tags.ljust(tags_width)}  {description}"
        for score, path, tags, description in rows
    )
    return "\n".join(lines)


# meiva:note
# The text `meiva memory search` prints for the gateway's answer: the `results` list as one JSON
# line, or as an aligned table whose header stays when nothing matched. An answer that is not
# an object with a `results` list raises `TypeError`; the shell lets it surface.
def render_results(payload: object, *, json_output: bool) -> str:
    if not _is_object_dict(payload):
        raise TypeError("memory search response must be an object")
    raw_results: object = payload["results"]
    if not _is_object_list(raw_results):
        raise TypeError("memory search response results must be a list")
    return json.dumps(raw_results) if json_output else _table(raw_results)
