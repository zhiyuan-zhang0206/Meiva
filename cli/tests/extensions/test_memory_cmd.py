# meiva:note
# `meiva memory search` and `meiva memory refresh`, end to end against a real listener.
#
# The gateway is the scripted HTTP server of the cluster command tests, on a loopback port that
# the verbs dial through their real transport and real headers. What the verb sent is read from
# what the server received and what it printed from the captured streams. The request body and
# the rendering are decided in `plans/memory_search.py` and asserted in
# `plans/tests/test_memory_search.py`; the shell proves it dials the right endpoint with the
# gateway credential, echoes a refusal's text, maps the answer to an exit status and prints
# what the plan renders, and the handler test starts from the parsed command line and the
# command's own configuration, so the wiring from `meiva memory search` to the dial is covered.

from __future__ import annotations

import json

import httpx
import pytest

from base.host.net.tests.local_http import closed_port_url
from cli import parsers as _parsers
from cli.commands.cluster.tests.gateway_server import SECRET, Reply, access_to, serving
from cli.commands.extensions.memory import cmd_memory_refresh, cmd_memory_search
from cli.commands.extensions.parsers import mcp as _mcp
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime

_SEARCH = ("POST", "/api/memory/search")
_REFRESH = ("POST", "/api/memory/refresh")
_ALPHA = {
    "path": "projects/alpha.md",
    "tags": ["project", "active"],
    "description": "Alpha context",
    "score": 0.91,
    "chunk": "Alpha is the active project.",
}


def test_a_search_posts_the_query_with_the_gateway_credential_and_prints_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving(
        {_SEARCH: Reply(body={"paths": [_ALPHA["path"]], "results": [_ALPHA]})}
    ) as gateway:
        rc = cmd_memory_search("alpha", limit=7, json_output=False, access=access_to(gateway.url))

    assert rc == 0
    (seen,) = gateway.requests
    assert seen.target == "/api/memory/search"
    assert seen.body == {"query": "alpha", "k": 7, "purpose": "search"}
    assert seen.headers["authorization"] == f"Bearer {SECRET}"
    assert capsys.readouterr().out.splitlines() == [
        "score  path               tags             description",
        "0.91   projects/alpha.md  project, active  Alpha context",
    ]


def test_json_output_prints_the_results_list_as_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({_SEARCH: Reply(body={"results": [_ALPHA]})}) as gateway:
        rc = cmd_memory_search("alpha", limit=5, json_output=True, access=access_to(gateway.url))

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [_ALPHA]


def test_a_search_that_matches_nothing_prints_the_header_and_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({_SEARCH: Reply(body={"results": []})}) as gateway:
        rc = cmd_memory_search("nothing", limit=5, json_output=False, access=access_to(gateway.url))

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["score  path  tags  description"]


@pytest.mark.parametrize(
    ("status", "payload", "diagnostic"),
    [
        (401, {"detail": "invalid cluster secret"}, "invalid cluster secret"),
        (422, {"detail": "query is invalid"}, "query is invalid"),
        (503, {"reason": "memory index unavailable"}, "memory index unavailable"),
    ],
)
def test_a_refusal_echoes_the_gateways_text_and_raises(
    capsys: pytest.CaptureFixture[str], status: int, payload: object, diagnostic: str
) -> None:
    with (
        serving({_SEARCH: Reply(status=status, body=payload)}) as gateway,
        pytest.raises(httpx.HTTPStatusError) as refused,
    ):
        cmd_memory_search("alpha", limit=5, json_output=False, access=access_to(gateway.url))

    assert refused.value.response.status_code == status
    assert diagnostic in capsys.readouterr().err


def test_a_refresh_posts_to_the_gateway_and_reports_its_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({_REFRESH: Reply(body={"status": "queued"})}) as gateway:
        rc = cmd_memory_refresh(access=access_to(gateway.url))

    assert rc == 0
    assert [seen.target for seen in gateway.requests] == ["/api/memory/refresh"]
    assert capsys.readouterr().out.splitlines() == [
        "refreshing memory index...",
        "  index refreshed: queued",
    ]


def test_a_refresh_the_gateway_cannot_be_reached_for_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        cmd_memory_refresh(access=access_to(closed_port_url()))

    assert exited.value.code == 1
    assert "refresh failed" in capsys.readouterr().err


def test_memory_search_parser_defaults_and_flags() -> None:
    args = _parsers.parse_args(["memory", "search", "alpha"])
    assert args.func is _mcp.h_memory_search
    assert (args.query, args.limit, args.json) == ("alpha", 5, False)
    exact_query = "  beta\tgamma  "
    args = _parsers.parse_args(["memory", "search", exact_query, "--limit", "9", "--json"])
    assert (args.query, args.limit, args.json) == (exact_query, 9, True)


def test_memory_search_handler_searches_the_gateway_the_command_configuration_names(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    exact_query = "  beta\tgamma  "
    hit = {"path": "a.md", "tags": None, "description": None, "score": 0.5, "chunk": "x"}
    args = _parsers.parse_args(["memory", "search", exact_query, "--limit", "9", "--json"])
    with serving({("POST", "/api/memory/search"): Reply(body={"results": [hit]})}) as gateway:
        command_runtime.config.set_field("gateway_url", gateway.url)
        command_runtime.config.set_field("cluster_secret", SECRET)
        args.runtime = command_runtime

        assert _mcp.h_memory_search(args) == 0

    (seen,) = gateway.requests
    assert seen.body == {"query": exact_query, "k": 9, "purpose": "search"}
    assert seen.headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(capsys.readouterr().out) == [hit]


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
