from __future__ import annotations

import pytest

from base.host.net.browser_origins import (
    browser_origin_of,
    is_browser_origin_url,
    parse_browser_origins,
    primary_browser_origin,
    split_browser_origins,
)

_FIRST = "https://public.example"
_SECOND = "https://private.example:8443"
_ORIGINS = (_FIRST, _SECOND)


@pytest.mark.parametrize(
    ("raw", "entries"),
    [
        ("", []),
        ("  ", []),
        ("https://a.example", ["https://a.example"]),
        ("https://a.example, https://b.example", ["https://a.example", " https://b.example"]),
        ("https://a.example,", ["https://a.example", ""]),
    ],
)
def test_split_keeps_every_comma_separated_entry(raw: str, entries: list[str]) -> None:
    assert split_browser_origins(raw) == entries


@pytest.mark.parametrize(
    ("entry", "origin"),
    [
        ("https://console.example", "https://console.example"),
        ("https://Console.Example:443/", "https://console.example"),
        ("  https://console.example:8443  ", "https://console.example:8443"),
        ("https://[::1]:8443", "https://[::1]:8443"),
        ("https://192.0.2.7", "https://192.0.2.7"),
        ("https://internal.example.net", "https://internal.example.net"),
    ],
)
def test_an_entry_becomes_its_canonical_origin(entry: str, origin: str) -> None:
    assert parse_browser_origins([entry]) == (origin,)


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "console.example",
        "http://console.example",
        "ftp://console.example",
        "https://",
        "https://user@console.example",
        "https://user:pass@console.example",
        "https://console.example/api",
        "https://console.example/?x=1",
        "https://console.example?x=1",
        "https://console.example#frag",
        "https://console.example:",
        "https://console.example:99999",
        "https://console.example:port",
        "https://console .example",
        "https://console.example\nhttps://other.example",
        "https://console.example.",
        'https://console.example"</script>',
        "https://[not-an-address]",
        "https://konsoleé.example",
    ],
)
def test_an_entry_that_is_not_a_plain_https_origin_is_refused(entry: str) -> None:
    with pytest.raises(ValueError, match="https origin"):
        parse_browser_origins([entry])


def test_the_order_written_is_the_order_kept() -> None:
    assert parse_browser_origins([_SECOND, _FIRST]) == (_SECOND, _FIRST)


def test_a_repeated_origin_is_refused_even_when_written_differently() -> None:
    with pytest.raises(ValueError, match="repeat"):
        parse_browser_origins(["https://a.example", "https://A.example:443/"])


def test_no_entries_is_no_origins() -> None:
    assert parse_browser_origins([]) == ()


def test_the_primary_entry_is_the_first_one() -> None:
    assert primary_browser_origin(_ORIGINS) == _FIRST
    assert primary_browser_origin(()) is None


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Public.Example/api/auth/login?x=1", "https://public.example"),
        ("https://public.example:443/", "https://public.example"),
        ("http://public.example:80/", "http://public.example"),
        ("http://192.0.2.2:8800/api", "http://192.0.2.2:8800"),
        ("https://[::1]:443/", "https://[::1]"),
        ("/relative", None),
        ("", None),
        ("https://host:notaport/", None),
    ],
)
def test_the_origin_of_a_url_is_what_a_browser_serializes(url: str, origin: str | None) -> None:
    assert browser_origin_of(url) == origin


@pytest.mark.parametrize(
    ("page", "listed"),
    [
        ("https://public.example/control?agent_id=42", True),
        ("https://public.example:443/", True),
        ("https://private.example:8443/", True),
        ("https://private.example/", False),
        ("https://private.example:9443/", False),
        ("http://public.example/", False),
        ("https://other.example/", False),
        ("http://192.0.2.2:8800/", False),
        ("not a url", False),
    ],
    ids=[
        "first",
        "first-explicit-default-port",
        "second",
        "second-without-its-port",
        "second-on-another-port",
        "listed-host-over-http",
        "unlisted-host",
        "direct-address",
        "not-a-url",
    ],
)
def test_a_page_is_an_entry_only_when_its_origin_is_listed(page: str, listed: bool) -> None:
    assert is_browser_origin_url(page, _ORIGINS) is listed


def test_no_listed_origin_means_no_page_is_an_entry() -> None:
    assert not is_browser_origin_url("https://public.example/", ())
