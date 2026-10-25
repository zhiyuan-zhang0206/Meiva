# meiva:note
# What the DeepSeek Search and Fetch tools are sent and how their answers are read, as plain
# values. Both tools take a JSON POST authenticated with `DEEPSEEK_API_KEY`.
# meiva:link meiva/web/docs/providers.meiva.okf.md

from __future__ import annotations

import json
from typing import TypeGuard

from pydantic import SecretStr

from meiva.web.configs import DeepSeekFetchConfig, DeepSeekSearchConfig
from meiva.web.errors import FetchError, SearchError, WebError
from meiva.web.wire import Hit, HttpRequest, Page

__all__ = [
    "deepseek_fetch_request",
    "deepseek_page",
    "deepseek_search_hits",
    "deepseek_search_request",
]


def _is_object_list(val: object) -> TypeGuard[list[object]]:
    return isinstance(val, list)


def _is_str_dict(val: object) -> TypeGuard[dict[str, object]]:
    return isinstance(val, dict)


def _key(api_key: SecretStr | None, error_type: type[WebError]) -> str:
    if api_key is None:
        raise error_type(
            "DEEPSEEK_API_KEY is not set — DeepSeek Search & Fetch uses the same key as the "
            "deepseek-* models; export it or write it into .env"
        )
    return api_key.get_secret_value()


def _post(endpoint: str, body: dict[str, object], api_key: str) -> HttpRequest:
    return HttpRequest(
        url=endpoint,
        query=(),
        suffix="",
        headers=(
            ("Authorization", f"Bearer {api_key}"),
            ("Content-Type", "application/json"),
            ("Accept", "application/json"),
        ),
        data=json.dumps(body).encode(),
        method="POST",
    )


# meiva:note
# The DeepSeek search for `query`, `count` clamped to 1 and the provider cap.
#
# Raises:
#     SearchError: no DeepSeek API key is configured.
def deepseek_search_request(config: DeepSeekSearchConfig, query: str, count: int) -> HttpRequest:
    return _post(
        config.endpoint,
        {"query": query, "count": max(1, min(count, config.max_count))},
        _key(config.api_key, SearchError),
    )


# meiva:note
# The hits of a DeepSeek answer: one list, `kind` always `web`, the summary standing in for an
# empty snippet.
def deepseek_search_hits(data: dict[str, object]) -> list[Hit]:
    raw_results = data.get("results")
    results = raw_results if _is_object_list(raw_results) else []
    return [
        Hit(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            snippet=str(item.get("snippet") or item.get("summary") or ""),
            kind="web",
        )
        for item in results
        if _is_str_dict(item)
    ]


# meiva:note
# The DeepSeek read of the page at `url`.
#
# Raises:
#     FetchError: no DeepSeek API key is configured.
def deepseek_fetch_request(config: DeepSeekFetchConfig, url: str) -> HttpRequest:
    return _post(config.endpoint, {"url": url}, _key(config.api_key, FetchError))


# meiva:note
# The page a DeepSeek answer carries, its body cut to `max_chars`.
#
# Raises:
#     FetchError: the answer has no page content.
def deepseek_page(data: dict[str, object], url: str, max_chars: int) -> Page:
    raw_content = data.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    if not content:
        raise FetchError(f"DeepSeek Fetch returned no page content for {url}")
    raw_title = data.get("title")
    title = raw_title if isinstance(raw_title, str) else ""
    return Page(title, url, content[:max_chars], len(content) > max_chars)
