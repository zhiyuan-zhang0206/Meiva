# meiva:note
# Whether `meiva start` mints a one-time browser login link, and what it prints.
#
# The link is `<ui>/#ticket=<ticket>`: a short-lived, single-use ticket in the URL *fragment*, which
# never reaches a server log or a Referer. The link is for a person at a terminal: a start whose
# output is not a terminal (a supervisor, a rollout child, a pipe) mints nothing, so no ticket lands
# in a log, and a unit without the human secret (a pure agent-runner) has no UI to link.

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

__all__ = [
    "LinkFacts",
    "LinkPlan",
    "MintLink",
    "NoLink",
    "SkipLink",
    "link_lines",
    "mint_failure_line",
    "plan_login_link",
    "ui_base",
]


# meiva:note
# What decides the link: the services this start launched and the UI addresses configured.
@dataclass(frozen=True)
class LinkFacts:
    sessions: frozenset[str]
    browser_origin: str
    entry_url: str
    app_port: int | None
    has_human_secret: bool
    at_terminal: bool


# meiva:note
# No UI to link, or no secret to mint it with: print nothing.
@dataclass(frozen=True)
class NoLink: ...


# meiva:note
# A link that would apply but is withheld; `line` says why.
@dataclass(frozen=True)
class SkipLink:
    line: str


# meiva:note
# Mint a ticket at the gateway and print the link to the UI at `base`.
@dataclass(frozen=True)
class MintLink:
    base: str


LinkPlan = NoLink | SkipLink | MintLink


# meiva:note
# Where a browser reaches the UI among the launched `sessions`, or None if none.
#
# Gate owns the public entry (the HTTPS browser origin when one is configured, else the
# entry URL); without Gate the Next.js app is reached directly on its reserved port.
def ui_base(
    sessions: Collection[str], *, browser_origin: str, entry_url: str, app_port: int | None
) -> str | None:
    if "gate" in sessions:
        return (browser_origin or entry_url).rstrip("/")
    if "frontend" in sessions and app_port is not None:
        return f"http://localhost:{app_port}"
    return None


# meiva:note
# What the start does about a login link.
def plan_login_link(facts: LinkFacts) -> LinkPlan:
    base = ui_base(
        facts.sessions,
        browser_origin=facts.browser_origin,
        entry_url=facts.entry_url,
        app_port=facts.app_port,
    )
    if base is None or not facts.has_human_secret:
        return NoLink()
    if not facts.at_terminal:
        return SkipLink("\n→ one-time UI login link skipped (output is not a terminal)")
    return MintLink(base)


# meiva:note
# The printed link; `ticket` is already safe for a URL fragment.
def link_lines(base: str, ticket: str) -> tuple[str, ...]:
    return (
        "\n→ open the UI (one-time login link, expires in two minutes):",
        f"  {base}/#ticket={ticket}",
    )


# meiva:note
# A gateway failure is reported and never fails the start: the web password still works.
def mint_failure_line(reason: str) -> str:
    return f"\n  ! could not mint a one-time UI login link: {reason}"
