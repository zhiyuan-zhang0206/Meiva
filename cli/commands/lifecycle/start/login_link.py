# meiva:note
# The one-time browser login link `meiva start` prints once the UI is ready.
#
# The ticket comes from the gateway's `POST /api/auth/login-ticket`, called with the home's human
# cluster secret, and the browser redeems it for the ordinary session cookie
# (gateway/http/auth/docs/web-sessions.meiva.okf.md). `login_link_plan` decides whether and where
# to mint; this module reads the facts, calls the gateway and prints.

from __future__ import annotations

import sys
from collections.abc import Collection
from urllib.parse import quote

from cli.commands.lifecycle.start.login_link_plan import (
    LinkFacts,
    MintLink,
    SkipLink,
    link_lines,
    mint_failure_line,
    plan_login_link,
)
from cli.commands.lifecycle.start.start_config import StartConfig

_TIMEOUT_S = 10.0


# meiva:note
# A fresh login ticket from this home's gateway.
#
# Raises:
#     httpx.HTTPError: the gateway is unreachable or refused the request.
#     ValueError: the gateway answered without a ticket.
def _issue_ticket(config: StartConfig) -> str:
    from base.cluster.machine import gateway_api_base
    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import post as dial_post

    response = dial_post(
        f"{gateway_api_base(config.gateway_url)}/api/auth/login-ticket",
        headers=bearer_header(config.data_plane.cluster_secret),
        timeout=_TIMEOUT_S,
    )
    response.raise_for_status()
    ticket = response.json().get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise ValueError("the gateway answered without a login ticket")
    return ticket


# meiva:note
# Print the one-time UI login link, or say why there is none.
#
# Inapplicable cases (no UI launched, no human secret, output not a terminal) print nothing
# or one line; a gateway failure is reported on stderr and never fails the start.
def print_login_link(sessions: Collection[str], *, config: StartConfig, at_terminal: bool) -> None:
    import httpx

    step = plan_login_link(
        LinkFacts(
            sessions=frozenset(sessions),
            browser_origin=config.browser_origin,
            entry_url=config.entry_url,
            app_port=config.app_port,
            has_human_secret=bool(config.data_plane.cluster_secret),
            at_terminal=at_terminal,
        )
    )
    if isinstance(step, SkipLink):
        print(step.line)
    elif isinstance(step, MintLink):
        try:
            ticket = _issue_ticket(config)
        except (httpx.HTTPError, ValueError) as exc:
            print(mint_failure_line(str(exc)), file=sys.stderr)
            return
        for line in link_lines(step.base, quote(ticket, safe=".-_")):
            print(line)
