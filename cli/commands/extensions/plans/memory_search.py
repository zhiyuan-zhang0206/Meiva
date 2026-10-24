# meiva:note
# `meiva memory search`: the request the gateway is asked and the text its answer becomes.
#
# Values in, text out. The shell dials the gateway and prints; this module decides the request
# body and the rendering, and refuses an answer that is not the documented shape.

from __future__ import annotations

import json
from typing import TypedDict, cast

__all__ = ["render_results", "search_body"]


class _MemorySearchResult(TypedDict):
    path: str
    tags: list[str] | None
    description: str | None
    score: float


# meiva:note
# The JSON body of `POST /api/memory/search`: `limit` travels as `k`, and the purpose names the
# caller so the gateway can tell a person's search from an agent's recall.
def search_body(query: str, limit: int) -> dict[str, str | int]:
    return {"query": query, "k": limit, "purpose": "search"}


# meiva:note
# Nullable display metadata becomes empty text for the table; the JSON rendering keeps the
# answer untouched, nulls included.
def _rows(results: list[_MemorySearchResult]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for result in results:
        if not isinstance(result, dict):
            raise TypeError("memory search results must contain objects")
        raw_tags = result.get("tags")
        tags = ", ".join(str(tag) for tag in raw_tags) if raw_tags else ""
        raw_description = result.get("description")
        description = "" if raw_description is None else str(raw_description)
        rows.append((f"{result['score']:.2f}", result["path"], tags, description))
    return rows


def _table(results: list[_MemorySearchResult]) -> str:
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
    if not isinstance(payload, dict):
        raise TypeError("memory search response must be an object")
    raw_results: object = cast(dict[str, object], payload)["results"]
    if not isinstance(raw_results, list):
        raise TypeError("memory search response results must be a list")
    results = cast(list[_MemorySearchResult], raw_results)
    return json.dumps(results) if json_output else _table(results)
