# meiva:note
# The server entry `meiva mcp add` / `install` build and name, decided from values
# (`plans/mcp_server_spec.py`). The commands that read and write the config around it are
# exercised in `test_mcp.py` and `test_mcp_install.py`.

from __future__ import annotations

import pytest

from cli.commands.extensions.plans.mcp_server_spec import (
    build_server_spec,
    derive_server_name,
    parse_env_pairs,
    server_summary,
)


def test_a_pasted_object_is_the_entry_as_given() -> None:
    spec = '{"command": "npx", "args": ["-y", "server-foo"], "env": {"K": "v"}}'

    assert build_server_spec(spec, None, [], []) == {
        "command": "npx",
        "args": ["-y", "server-foo"],
        "env": {"K": "v"},
    }


def test_flags_build_the_same_shape_and_omit_what_was_not_given() -> None:
    assert build_server_spec(None, "uvx", ["server-bar"], ["TOKEN=abc"]) == {
        "command": "uvx",
        "args": ["server-bar"],
        "env": {"TOKEN": "abc"},
    }
    assert build_server_spec(None, "uvx", [], []) == {"command": "uvx"}


@pytest.mark.parametrize(
    ("json_spec", "command", "env_pairs", "reason"),
    [
        ('{"command": "c"}', "c", [], "not both"),
        (None, None, [], "need --json"),
        ("[1, 2]", None, [], "server object"),
        (None, "c", ["NOEQUALS"], "KEY=VALUE"),
    ],
)
def test_input_that_is_not_one_server_entry_is_refused(
    json_spec: str | None, command: str | None, env_pairs: list[str], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        build_server_spec(json_spec, command, [], env_pairs)


def test_malformed_json_is_a_value_error_too() -> None:
    with pytest.raises(ValueError, match="Expecting"):
        build_server_spec("{not json}", None, [], [])


def test_the_first_equals_splits_an_env_pair() -> None:
    assert parse_env_pairs(["A=1", "URL=http://h/?q=2"]) == {"A": "1", "URL": "http://h/?q=2"}


@pytest.mark.parametrize(
    ("spec", "summary"),
    [
        ({"url": "https://mcp.example/sse", "command": "ignored"}, "https://mcp.example/sse"),
        ({"command": "npx", "args": ["-y", "server-foo"]}, "npx -y server-foo"),
        ({"command": "server-foo"}, "server-foo"),
        ({"command": "c", "args": "not-a-list"}, "c"),
        ("not an object", "(malformed entry)"),
    ],
)
def test_the_list_view_summarizes_url_else_command_and_args(spec: object, summary: str) -> None:
    assert server_summary(spec) == summary


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("meiva-mcp-widget", "widget"),
        ("mcp-server-foo_bar", "foo-bar"),
        ("MCP-fallback", "fallback"),
        ("plain_name", "plain-name"),
        ("-edge-", "edge"),
    ],
)
def test_a_server_is_named_from_its_package_name(raw: str, name: str) -> None:
    assert derive_server_name(raw) == name
