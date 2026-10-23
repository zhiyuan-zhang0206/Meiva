# meiva:note
# Whether `meiva start` mints a one-time login link, and where the link points.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.start.login_link_plan import (
    LinkFacts,
    MintLink,
    NoLink,
    SkipLink,
    link_lines,
    mint_failure_line,
    plan_login_link,
    ui_base,
)


def _facts(
    sessions: set[str],
    *,
    secret: bool = True,
    terminal: bool = True,
    origin: str = "",
    app_port: int | None = None,
) -> LinkFacts:
    return LinkFacts(
        sessions=frozenset(sessions),
        browser_origin=origin,
        entry_url="http://localhost:3000",
        app_port=app_port,
        has_human_secret=secret,
        at_terminal=terminal,
    )


def test_ui_base_is_the_gate_entry_or_the_https_browser_origin() -> None:
    entry = "http://localhost:3000"
    assert ui_base({"gateway", "gate"}, browser_origin="", entry_url=entry, app_port=None) == entry
    assert (
        ui_base({"gate"}, browser_origin="https://console.example/", entry_url=entry, app_port=None)
        == "https://console.example"
    )


def test_ui_base_without_gate_is_the_apps_reserved_port() -> None:
    entry = "http://localhost:3000"
    assert (
        ui_base({"gateway", "frontend"}, browser_origin="", entry_url=entry, app_port=None) is None
        # meiva:note
        # no reserved port to point at
    )
    assert (
        ui_base({"gateway", "frontend"}, browser_origin="", entry_url=entry, app_port=3101)
        == "http://localhost:3101"
    )
    assert ui_base({"gateway"}, browser_origin="", entry_url=entry, app_port=3101) is None


def test_a_terminal_with_a_ui_and_the_secret_mints_a_link() -> None:
    assert plan_login_link(_facts({"gateway", "gate"})) == MintLink("http://localhost:3000")


def test_output_that_is_not_a_terminal_withholds_the_link_and_says_so() -> None:
    plan = plan_login_link(_facts({"gateway", "gate"}, terminal=False))

    assert plan == SkipLink("\n→ one-time UI login link skipped (output is not a terminal)")


@pytest.mark.parametrize(
    "facts",
    [
        _facts({"gateway"}),
        _facts({"gateway", "gate"}, secret=False),
        _facts({"gateway"}, terminal=False),
    ],
    ids=["no ui launched", "no human secret", "no ui and no terminal"],
)
def test_nothing_is_said_without_a_ui_or_the_human_secret(facts: LinkFacts) -> None:
    assert plan_login_link(facts) == NoLink()


def test_the_link_carries_the_ticket_in_the_fragment() -> None:
    assert link_lines("http://localhost:3000", "ticket.human-0123") == (
        "\n→ open the UI (one-time login link, expires in two minutes):",
        "  http://localhost:3000/#ticket=ticket.human-0123",
    )


def test_a_mint_failure_is_a_warning_line() -> None:
    assert mint_failure_line("refused") == "\n  ! could not mint a one-time UI login link: refused"
