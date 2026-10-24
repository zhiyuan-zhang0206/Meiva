# meiva:note
# What the web-scrape adapter asks of its HTTP client and how it reads the answer, as plain
# values: the argv that fetches a page or asks Jina Reader to render one, the text a finished
# process yields or the failure it means, and the tripwire that keeps an agent-supplied URL off
# internal hosts. Nothing here starts a process, reads the environment or touches the network;
# `webscrape.py` runs the argv, and the rss adapter shares the argv and the tripwire.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass

# meiva:note
# The HTTP client program every adapter runs. It is a value so a composition root names it once
# and a test can point it at a stand-in program.
CURL = "curl"

# meiva:note
# Jina Reader: a server-side fetch that runs a real headless browser + JS render and returns the
# page as clean markdown. The escalation rung above plain curl for sources that gate a bare
# client behind an anti-bot / JS-render wall (Cloudflare challenge, "enable JavaScript" shell).
# Anonymous works for much of the open web; a JINA_API_KEY adds proxy rotation + forced render
# for the harder hosts and lifts the rate limit.
JINA_BASE = "https://r.jina.ai/"

# meiva:note
# An honest browser UA: a bare client UA is 403'd / timed out by many news and forum sites that
# answer a normal browser request (verified against Chinese official / forum sites that 403 the
# WebFetch tool but 200 a Chrome UA via curl).
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

_JINA_KEY_VAR = "JINA_API_KEY"


# meiva:note
# A page could not be fetched (bad scheme, blocked host, curl failure) or yielded no extractable
# article body.
class WebScrapeError(Exception):
    pass


# meiva:note
# How a scrape run reaches the web: the HTTP client program, the Jina Reader endpoint prefix and
# the optional Jina key. A composition root builds it once from the process environment.
@dataclass(frozen=True)
class ScrapeConfig:
    curl: str
    jina_base: str
    jina_api_key: str | None


# meiva:note
# The scrape config an environment describes: the stock client and endpoint, and the Jina key
# when `JINA_API_KEY` is set.
def config_from_env(env: Mapping[str, str]) -> ScrapeConfig:
    return ScrapeConfig(curl=CURL, jina_base=JINA_BASE, jina_api_key=env.get(_JINA_KEY_VAR))


# meiva:note
# Why `url` may not be fetched, or None when it may. This is defense in depth for an
# agent-supplied URL: it refuses a non-http(s) scheme, a `localhost` name and a literal
# loopback, private, link-local or reserved IP. A hostname that resolves to a private IP is not
# chased (that needs a resolve); the agent driving this is trusted, so this is a tripwire, not a
# sandbox. `noun` names the URL in the message ("URL", "feed URL"). A trailing dot names the
# same host (`localhost.`), so it is dropped before the comparison.
def refusal(url: str, scheme: str, host: str | None, *, noun: str) -> str | None:
    if scheme not in ("http", "https"):
        return f"refusing non-http(s) {noun}: {url!r}"
    name = (host or "").lower().rstrip(".")
    if name in ("localhost", "localhost.localdomain") or name.endswith(".localhost"):
        return f"refusing internal host: {name!r}"
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return f"refusing internal IP: {name!r}"
    return None


# meiva:note
# The argv that fetches `url` with `curl`: a browser UA, redirects capped at five, compression
# on, 45 seconds, and `-f` so an HTTP error fails the process.
def curl_argv(curl: str, url: str) -> tuple[str, ...]:
    return (curl, "-fsSL", "--max-redirs", "5", "--compressed", "--max-time", "45", "-A", _UA, url)


# meiva:note
# The argv that fetches `endpoint` (the Jina base plus the percent-quoted page URL) with `curl`,
# carrying the Jina key as a bearer token when the config has one.
def jina_argv(config: ScrapeConfig, endpoint: str) -> tuple[str, ...]:
    auth = ("-H", f"Authorization: Bearer {config.jina_api_key}") if config.jina_api_key else ()
    return (config.curl, "-fsSL", "--max-time", "60", "-A", _UA, *auth, endpoint)


# meiva:note
# The page a finished `curl` produced, as text.
#
# Raises:
#     WebScrapeError: the process failed (`-f` turns an HTTP error into a nonzero exit).
def html_text(url: str, returncode: int, stdout: bytes, stderr: bytes) -> str:
    if returncode != 0:
        raise WebScrapeError(
            f"curl failed for {url} (rc={returncode}): {stderr.decode(errors='replace')[:200]}"
        )
    return stdout.decode("utf-8", errors="replace")


# meiva:note
# The markdown a finished Jina render produced, as text.
#
# Raises:
#     WebScrapeError: the render call failed, or the target itself refused the render (Jina
#         echoes e.g. "Target URL returned error 403: Forbidden").
def render_text(url: str, returncode: int, stdout: bytes, stderr: bytes) -> str:
    if returncode != 0:
        raise WebScrapeError(
            f"jina render failed for {url} (rc={returncode}): "
            f"{stderr.decode(errors='replace')[:200]}"
        )
    raw = stdout.decode("utf-8", errors="replace")
    if "Target URL returned error" in raw:
        raise WebScrapeError(
            f"jina render of {url}: the target refused the render ({raw.splitlines()[0]})"
        )
    return raw
