from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from base.cluster.machine.tests.gateway_stubs import (
    gateway_api_base_stub,
    gateway_auth_headers_stub,
)
from cli import parsers as _parsers
from cli.commands.extensions import memory as _memory
from cli.commands.extensions.parsers import mcp as _mcp


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://gw:8000/api/memory/search")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self) -> object:
        return self._payload


_API_BASE = gateway_api_base_stub("http://gw:8000")
_AUTH_HEADERS = gateway_auth_headers_stub({"Authorization": "Bearer secret"})


def _make_post(
    payload: object, *, status_code: int = 200
) -> tuple[dict[str, Any], Callable[..., _FakeResp]]:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResp:
        seen["url"] = url
        seen.update(kwargs)
        return _FakeResp(payload, status_code=status_code)

    return seen, fake_post


def _run_search(
    query: str,
    *,
    limit: int,
    json_output: bool,
    post_fn: Callable[..., _FakeResp],
) -> int:
    return _memory.cmd_memory_search(
        query,
        limit=limit,
        json_output=json_output,
        api_base_fn=_API_BASE,
        auth_headers_fn=_AUTH_HEADERS,
        post_fn=post_fn,
    )


def test_memory_search_posts_authenticated_query_and_renders_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results: list[dict[str, object]] = [
        {
            "path": "projects/alpha.md",
            "tags": ["project", "active"],
            "description": "Alpha context",
            "score": 0.91,
            "chunk": "Alpha is the active project.",
        },
        {"path": "notes/empty.md", "tags": [], "description": "", "score": 0.5, "chunk": ""},
    ]
    seen, post_fn = _make_post(
        {
            "paths": ["projects/alpha.md", "notes/empty.md"],
            "results": results,
        },
    )

    assert _run_search("alpha", limit=7, json_output=False, post_fn=post_fn) == 0
    assert seen["url"] == "http://gw:8000/api/memory/search"
    assert seen["json"] == {"query": "alpha", "k": 7, "purpose": "search"}
    assert seen["headers"] == {"Authorization": "Bearer secret"}
    assert capsys.readouterr().out.splitlines() == [
        "score  path               tags             description",
        "0.91   projects/alpha.md  project, active  Alpha context",
        "0.50   notes/empty.md                      ",
    ]


def test_memory_search_json_emits_results_shape_and_preserves_nulls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results: list[dict[str, object]] = [
        {"path": "relative/note.md", "tags": None, "description": None, "score": 0.8, "chunk": "x"},
        {"path": "relative/empty.md", "tags": [], "description": "", "score": 0.1, "chunk": "y"},
    ]
    _seen, post_fn = _make_post(
        {"paths": [row["path"] for row in results], "results": results},
    )

    assert _run_search("edge cases", limit=5, json_output=True, post_fn=post_fn) == 0
    assert json.loads(capsys.readouterr().out) == results


def test_memory_search_human_output_renders_null_metadata_as_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seen, post_fn = _make_post(
        {
            "paths": ["relative/note.md"],
            "results": [
                {
                    "path": "relative/note.md",
                    "tags": None,
                    "description": None,
                    "score": 0.25,
                    "chunk": "x",
                },
            ],
        },
    )

    assert _run_search("edge cases", limit=5, json_output=False, post_fn=post_fn) == 0
    assert capsys.readouterr().out.splitlines() == [
        "score  path              tags  description",
        "0.25   relative/note.md        ",
    ]


@pytest.mark.parametrize(
    ("status_code", "payload", "diagnostic"),
    [
        (401, {"detail": "invalid cluster secret"}, "invalid cluster secret"),
        (422, {"detail": "query is invalid"}, "query is invalid"),
        (503, {"reason": "memory index unavailable"}, "memory index unavailable"),
    ],
)
def test_memory_search_surfaces_gateway_error_detail(
    capsys: pytest.CaptureFixture[str],
    status_code: int,
    payload: object,
    diagnostic: str,
) -> None:
    _seen, post_fn = _make_post(payload, status_code=status_code)

    with pytest.raises(httpx.HTTPStatusError):
        _run_search("alpha", limit=5, json_output=False, post_fn=post_fn)

    assert diagnostic in capsys.readouterr().err


def test_memory_search_parser_defaults_and_forwards_flags() -> None:
    calls: list[tuple[str, int, bool]] = []

    def search(query: str, *, limit: int, json_output: bool) -> int:
        calls.append((query, limit, json_output))
        return 0

    args = _parsers.parse_args(["memory", "search", "alpha"])
    assert args.func is _mcp.h_memory_search
    assert _mcp.h_memory_search(args, search_fn=search) == 0
    exact_query = "  beta\tgamma  "
    args = _parsers.parse_args(["memory", "search", exact_query, "--limit", "9", "--json"])
    assert _mcp.h_memory_search(args, search_fn=search) == 0
    assert calls == [("alpha", 5, False), (exact_query, 9, True)]


@pytest.mark.parametrize("query", ["", "   ", "\t", "\n"])
def test_memory_search_parser_rejects_empty_or_whitespace_query(query: str) -> None:
    with pytest.raises(SystemExit) as exited:
        _parsers.parse_args(["memory", "search", query])

    assert exited.value.code == 2


@pytest.mark.parametrize("limit", ["0", "101"])
def test_memory_search_parser_rejects_out_of_range_limit(limit: str) -> None:
    with pytest.raises(SystemExit) as exited:
        _parsers.parse_args(["memory", "search", "alpha", "--limit", limit])

    assert exited.value.code == 2


@pytest.mark.parametrize("limit", ["1", "100"])
def test_memory_search_parser_accepts_limit_bounds(limit: str) -> None:
    args = _parsers.parse_args(["memory", "search", "alpha", "--limit", limit])

    assert args.limit == int(limit)
