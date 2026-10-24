# meiva:note
# What the Brave and Jina providers are sent and how their answers are read, as plain values: the
# request for a query or a page, the hits and page bodies a JSON answer carries, and the model
# call that answers a prompt about a page. Nothing here touches the network, the clock or the
# process; `transport.py` sends a request and the provider modules join the two.
# meiva:link meiva/web/docs/providers.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from base.lm.effort import ReasoningEffort
from meiva.sdk_surface.pure.media.finding_plan import Findings, findings_of
from meiva.web.configs import BraveConfig, JinaConfig
from meiva.web.errors import FetchError, SearchError

__all__ = [
    "AnswerRequest",
    "Hit",
    "HttpRequest",
    "Page",
    "brave_hits",
    "brave_request",
    "hit_findings",
    "jina_page",
    "jina_request",
    "plan_answer",
]

# meiva:note
# Python urllib default UA (`Python-urllib/3.x`) is blocked by Cloudflare
# (`browser_signature_banned`); r.jina.ai sits behind Cloudflare — no UA
# results in 403. An honest identifier UA passes through. Brave Search API
# does not go through Cloudflare and is unaffected.
_MEIVA_USER_AGENT = "meiva-sdk/1.0"

# meiva:note
# A reader fetch can come back HTTP 200 with the site's bot-challenge or
# login-wall page as the body instead of the article. The body then looks like
# a success, so without this check the wall text is fed to the model, which
# "answers" the prompt from a verification notice (seen on Cloudflare-protected
# sites and login-gated social sites). These markers identify the common walls;
# a wall body is always tiny, so a length cap keeps an article that merely
# mentions one of the phrases from being flagged.
_WALL_MARKERS = (
    "just a moment...",
    "performing security verification",
    "security service to protect against malicious bots",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "verify you are human",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u8bf7\u60a8\u767b\u5f55\u540e\u67e5\u770b",
    "\u767b\u5f55\u540e\u67e5\u770b\u66f4\u591a",
)
_WALL_MAX_LEN = 2_000

# meiva:note
# Brave answers with one section per result type; each is collected into the flat hit list,
# tagged with its section name as `kind`. Per the API docs only `type` and `query` are
# guaranteed top-level keys, and a section appears only when it has data.
_BRAVE_SECTIONS = ("web", "news", "videos", "discussions")


# meiva:note
# One request as its parts, before any encoding: the endpoint, the query parameters appended
# after `?`, a `suffix` that is percent-quoted and appended to the endpoint (the page URL of a
# Jina read), headers in order, an optional body and the method.
@dataclass(frozen=True)
class HttpRequest:
    url: str
    query: tuple[tuple[str, str], ...]
    suffix: str
    headers: tuple[tuple[str, str], ...]
    data: bytes | None
    method: str


# meiva:note
# One search result, before the facade gives it its agent-visible type.
@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str
    kind: str


# meiva:note
# A fetched page: its title, URL, body cut to the caller's limit, and whether it was cut.
@dataclass(frozen=True)
class Page:
    title: str
    url: str
    content: str
    truncated: bool


# meiva:note
# The model call that answers `prompt` about a page: model, effort and the two texts.
@dataclass(frozen=True)
class AnswerRequest:
    model: str
    effort: str | ReasoningEffort
    desc: str
    url: str
    material: str
    prompt: str


# meiva:note
# The Brave search for `query`, `count` clamped to 1 and the provider cap.
#
# Raises:
#     SearchError: no Brave API key is configured.
def brave_request(config: BraveConfig, query: str, count: int) -> HttpRequest:
    if config.api_key is None:
        raise SearchError(
            "BRAVE_API_KEY environment variable not set — register for a free tier key at "
            "https://brave.com/search/api/ then export it or write it into .env"
        )
    return HttpRequest(
        url=config.endpoint,
        query=(("q", query), ("count", str(max(1, min(count, config.max_count))))),
        suffix="",
        headers=(
            ("X-Subscription-Token", config.api_key.get_secret_value()),
            ("Accept", "application/json"),
        ),
        data=None,
        method="GET",
    )


# meiva:note
# The hits of every section of a Brave answer, in section order.
def brave_hits(data: dict[str, object]) -> list[Hit]:
    answer = cast("dict[str, Any]", data)
    hits: list[Hit] = []
    for section in _BRAVE_SECTIONS:
        for result in answer.get(section, {}).get("results", []):
            hits.append(
                Hit(
                    title=result.get("title", ""),
                    url=result.get("url", ""),
                    snippet=result.get("description", ""),
                    kind=section,
                )
            )
    return hits


# meiva:note
# The findings of the titles and snippets of `hits`, each hit's title before its snippet, in hit
# order.
def hit_findings(hits: Sequence[Hit]) -> Findings:
    return tuple(
        finding
        for hit in hits
        for text in (hit.title, hit.snippet)
        for finding in findings_of(text, "web.search")
    )


# meiva:note
# The Jina Reader read of the page at `url`; the key is optional (anonymous works, with a lower
# rate limit). The page URL is the request's suffix, quoted when the request is sent.
def jina_request(config: JinaConfig, url: str) -> HttpRequest:
    headers = [("Accept", "application/json"), ("User-Agent", _MEIVA_USER_AGENT)]
    if config.api_key is not None:
        headers.append(("Authorization", f"Bearer {config.api_key.get_secret_value()}"))
    return HttpRequest(
        url=config.reader_base,
        query=(),
        suffix=url,
        headers=tuple(headers),
        data=None,
        method="GET",
    )


def _raise_if_wall(url: str, title: str, content: str) -> None:
    blob = f"{title}\n{content}".lower()
    if len(content) > _WALL_MAX_LEN or not any(marker in blob for marker in _WALL_MARKERS):
        return
    raise FetchError(
        f"The reader returned a bot-challenge or login wall for {url} instead of "
        f"the page content (title={title!r})."
    )


# meiva:note
# The page a Jina Reader answer carries, its body cut to `max_chars`.
#
# Jina business errors (upstream URL unreachable or rejected) use `code` to tell the HTTP layer
# from the application layer; when it is not 200 the data fields may be empty, so it raises and
# the agent sees the specific reason. The answer has the shape `{code, status, data: {title,
# description, url, content, metadata, external, usage}}` (observed 2026-04).
#
# Raises:
#     FetchError: Jina reports a business error (`code` other than 200), or the body is a
#         bot-challenge or login wall instead of the page.
def jina_page(data: dict[str, object], url: str, max_chars: int) -> Page:
    answer = cast("dict[str, Any]", data)
    code = answer.get("code")
    if code != 200:
        msg = answer.get("message") or answer.get("status") or "unknown"
        raise FetchError(f"Jina Reader fetch failed (code={code}): {msg}")
    payload = answer.get("data", {})
    content = payload.get("content", "")
    title = payload.get("title", "")
    page_url = payload.get("url", url)
    _raise_if_wall(url, title, content)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return Page(title, page_url, content, truncated)


# meiva:note
# The model call that answers `prompt` about `page`. An explicit per-call `effort` wins over the
# configured web-fetch reasoning. The page is framed with its title and URL, and the model is
# told when the body was cut off.
def plan_answer(
    model: str, reasoning: str, page: Page, prompt: str, effort: str | ReasoningEffort | None
) -> AnswerRequest:
    cut = (
        "\n\n[The page was longer than the read limit and was cut off here.]"
        if page.truncated
        else ""
    )
    return AnswerRequest(
        model=model,
        effort=effort if effort is not None else reasoning,
        desc=f"{model} for {page.url}",
        url=page.url,
        material=f"# {page.title}\n{page.url}\n\n{page.content}{cut}",
        prompt=prompt,
    )
