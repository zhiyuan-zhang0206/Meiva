# meiva:note
# The pure side of meiva.web: what each provider is sent for a query or a page, what its answer
# carries, the model call that answers a prompt about a page, and the wording of a failure. The
# tests pass values and assert the returned request, hits, page or text; no network is involved.

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from base.lm.effort import ReasoningEffort
from meiva.web.configs import BraveConfig, DeepSeekFetchConfig, DeepSeekSearchConfig, JinaConfig
from meiva.web.deepseek_wire import (
    deepseek_fetch_request,
    deepseek_page,
    deepseek_search_hits,
    deepseek_search_request,
)
from meiva.web.errors import FetchError, SearchError
from meiva.web.failures import connection_failed_message, http_failed_message, non_json_message
from meiva.web.wire import (
    Hit,
    Page,
    brave_hits,
    brave_request,
    jina_page,
    jina_request,
    plan_answer,
)

_BRAVE = BraveConfig(
    api_key=SecretStr("my-secret-key"),
    endpoint="https://search.example/api",
    timeout_s=10.0,
    max_count=20,
)
_JINA = JinaConfig(api_key=None, reader_base="https://reader.example/", timeout_s=10.0)
_DS_SEARCH = DeepSeekSearchConfig(
    api_key=SecretStr("ds-key"),
    endpoint="https://ds.example/search",
    timeout_s=10.0,
    max_count=50,
    qps=5.0,
)
_DS_FETCH = DeepSeekFetchConfig(
    api_key=SecretStr("ds-key"), endpoint="https://ds.example/fetch", timeout_s=10.0, qps=5.0
)


def test_brave_request_carries_the_query_and_the_subscription_token() -> None:
    request = brave_request(_BRAVE, "python asyncio", 5)

    assert request.method == "GET"
    assert request.url == "https://search.example/api"
    assert request.query == (("q", "python asyncio"), ("count", "5"))
    assert dict(request.headers) == {
        "X-Subscription-Token": "my-secret-key",
        "Accept": "application/json",
    }
    assert request.data is None


@pytest.mark.parametrize(("asked", "sent"), [(100, "20"), (0, "1"), (-4, "1"), (7, "7")])
def test_brave_request_clamps_count_to_one_and_the_provider_cap(asked: int, sent: str) -> None:
    assert brave_request(_BRAVE, "x", asked).query[1] == ("count", sent)


def test_brave_request_needs_a_key() -> None:
    with pytest.raises(SearchError, match="BRAVE_API_KEY"):
        brave_request(BraveConfig(None, "https://x", 1.0, 20), "anything", 3)


def test_brave_hits_collect_every_section_tagged_with_its_kind() -> None:
    answer: dict[str, object] = {
        "web": {"results": [{"title": "W", "url": "https://a.com", "description": "dw"}]},
        "news": {"results": [{"title": "N", "url": "https://b.com", "description": "dn"}]},
        "videos": {"results": [{"title": "V", "url": "https://c.com", "description": "dv"}]},
    }

    assert brave_hits(answer) == [
        Hit("W", "https://a.com", "dw", "web"),
        Hit("N", "https://b.com", "dn", "news"),
        Hit("V", "https://c.com", "dv", "videos"),
    ]


def test_brave_hits_of_an_answer_without_sections_are_empty() -> None:
    assert brave_hits({"type": "search", "query": {"original": "q"}}) == []
    assert brave_hits({"web": {"results": []}}) == []


def test_jina_request_names_the_page_and_an_honest_user_agent() -> None:
    request = jina_request(_JINA, "https://example.com/search?q=hello world")

    assert request.url == "https://reader.example/"
    assert request.suffix == "https://example.com/search?q=hello world"
    headers = dict(request.headers)
    assert headers["Accept"] == "application/json"
    assert "meiva-sdk" in headers["User-Agent"]
    assert "Authorization" not in headers


def test_jina_request_authenticates_when_a_key_is_configured() -> None:
    keyed = JinaConfig(SecretStr("jina-secret"), "https://reader.example/", 10.0)

    assert dict(jina_request(keyed, "https://example.com").headers)["Authorization"] == (
        "Bearer jina-secret"
    )


def _jina(content: str, *, title: str = "", url: str = "", code: int = 200) -> dict[str, object]:
    return {"code": code, "data": {"title": title, "url": url, "content": content}}


def test_jina_page_cuts_the_body_and_reports_it() -> None:
    page = jina_page(_jina("x" * 100, title="T", url="https://p"), "https://example.com", 40)

    assert page == Page("T", "https://p", "x" * 40, True)


def test_jina_page_keeps_a_short_body_whole() -> None:
    assert jina_page(_jina("short"), "https://example.com", 1000) == Page("", "", "short", False)


def test_jina_page_business_error_raises_before_any_page_is_read() -> None:
    answer: dict[str, object] = {"code": 422, "message": "Unable to fetch: 404 Not Found"}

    with pytest.raises(FetchError, match=r"Jina Reader fetch failed \(code=422\)"):
        jina_page(answer, "https://example.com/nonexistent", 1000)


@pytest.mark.parametrize(
    ("title", "content"),
    [
        (
            "Just a moment...",
            "Performing security verification\nThis website uses a security service "
            "to protect against malicious bots.",
        ),
        (
            "\u5b89\u5168\u9a8c\u8bc1 - \u77e5\u4e4e",
            "\u8bf7\u60a8\u767b\u5f55\u540e\u67e5\u770b\u66f4\u591a\u4e13\u4e1a\u4f18\u8d28\u5185\u5bb9\u3002",
        ),
    ],
)
def test_jina_page_refuses_a_bot_challenge_or_login_wall(title: str, content: str) -> None:
    with pytest.raises(FetchError, match="bot-challenge or login wall"):
        jina_page(_jina(content, title=title), "https://example.com/a", 1000)


def test_jina_page_lets_a_long_article_that_mentions_a_wall_phrase_through() -> None:
    body = "An article about how Cloudflare shows 'Just a moment...' pages.\n" + ("x" * 5_000)

    page = jina_page(_jina(body, title="On bot challenges"), "https://example.com/blog", 10_000)

    assert "Just a moment" in page.content


def test_deepseek_search_request_is_an_authenticated_json_post() -> None:
    request = deepseek_search_request(_DS_SEARCH, "python asyncio", 5)

    assert request.method == "POST"
    assert request.url == "https://ds.example/search"
    assert dict(request.headers)["Authorization"] == "Bearer ds-key"
    assert request.data is not None
    assert json.loads(request.data) == {"query": "python asyncio", "count": 5}


def test_deepseek_search_request_clamps_count_to_the_provider_cap() -> None:
    request = deepseek_search_request(_DS_SEARCH, "q", 100)

    assert request.data is not None
    assert json.loads(request.data)["count"] == 50


def test_deepseek_requests_need_the_deepseek_key() -> None:
    with pytest.raises(SearchError, match="DEEPSEEK_API_KEY"):
        deepseek_search_request(
            DeepSeekSearchConfig(None, "https://ds.example/search", 1.0, 50, 5.0), "q", 3
        )
    with pytest.raises(FetchError, match="DEEPSEEK_API_KEY"):
        deepseek_fetch_request(
            DeepSeekFetchConfig(None, "https://ds.example/fetch", 1.0, 5.0), "https://p"
        )


def test_deepseek_fetch_request_names_the_page() -> None:
    request = deepseek_fetch_request(_DS_FETCH, "https://example.com/p")

    assert request.data is not None
    assert json.loads(request.data) == {"url": "https://example.com/p"}


def test_deepseek_search_hits_are_all_web_and_fall_back_to_the_summary() -> None:
    answer: dict[str, object] = {
        "results": [
            {"title": "t1", "url": "https://a", "snippet": "s1", "summary": "long1"},
            {"title": "t2", "url": "https://b", "snippet": "", "summary": "long2", "date": None},
        ]
    }

    assert deepseek_search_hits(answer) == [
        Hit("t1", "https://a", "s1", "web"),
        Hit("t2", "https://b", "long2", "web"),
    ]
    assert deepseek_search_hits({"results": None}) == []


def test_deepseek_page_is_cut_and_reports_it() -> None:
    page = deepseek_page({"title": "Page", "content": "x" * 500}, "https://example.com/p", 100)

    assert page == Page("Page", "https://example.com/p", "x" * 100, True)


def test_deepseek_page_refuses_an_empty_body() -> None:
    with pytest.raises(FetchError, match="no page content"):
        deepseek_page({"title": "t", "content": ""}, "https://example.com/p", 100)


def _page(*, truncated: bool = False) -> Page:
    return Page(
        "PEP 758", "https://peps.python.org/pep-0758/", "except* without parens.", truncated
    )


def test_the_answer_call_frames_the_page_with_its_title_and_url() -> None:
    call = plan_answer("deepseek-flash", "none", _page(), "what does this PEP propose?", None)

    assert call.model == "deepseek-flash"
    assert call.prompt == "what does this PEP propose?"
    assert call.material == (
        "# PEP 758\nhttps://peps.python.org/pep-0758/\n\nexcept* without parens."
    )
    assert call.desc == "deepseek-flash for https://peps.python.org/pep-0758/"


def test_the_answer_call_uses_the_configured_reasoning_unless_the_caller_gives_effort() -> None:
    page = _page()

    assert plan_answer("m", "high", page, "p", None).effort == "high"
    assert plan_answer("m", "high", page, "p", "low").effort == "low"
    assert plan_answer("m", "none", page, "p", ReasoningEffort.MAX).effort is ReasoningEffort.MAX


def test_the_answer_call_tells_the_model_when_the_page_was_cut() -> None:
    assert "cut off" in plan_answer("m", "none", _page(truncated=True), "p", None).material
    assert "cut off" not in plan_answer("m", "none", _page(), "p", None).material


def test_an_http_failure_carries_the_body_or_the_providers_json_message() -> None:
    body = '{"error": {"message": "Authentication Fails, Your api key is invalid"}}'

    assert http_failed_message("Brave Search", 429, body, provider_json=False) == (
        f"Brave Search HTTP 429: {body}"
    )
    assert http_failed_message("DeepSeek Search", 401, body, provider_json=True) == (
        "DeepSeek Search HTTP 401: Authentication Fails, Your api key is invalid"
    )


def test_a_json_error_without_a_message_falls_back_to_a_capped_body() -> None:
    assert (
        http_failed_message("X", 500, "plain text", provider_json=True) == "X HTTP 500: plain text"
    )
    long = "z" * 500
    assert http_failed_message("X", 500, long, provider_json=True) == f"X HTTP 500: {'z' * 200}"
    assert http_failed_message("X", 500, '{"error": "flat"}', provider_json=True).endswith(
        '{"error": "flat"}'
    )


def test_connection_and_decode_failures_are_worded_with_the_provider_label() -> None:
    assert connection_failed_message("Jina Reader", "refused") == (
        "Jina Reader connection failed: refused"
    )
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads("not json")
    assert non_json_message("Jina Reader", caught.value).startswith("Jina Reader returned non-JSON")
