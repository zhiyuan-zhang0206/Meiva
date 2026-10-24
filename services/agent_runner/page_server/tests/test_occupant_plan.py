# meiva:note
# Which local processes hold a page-server port, and which of them the daemon kills.

from __future__ import annotations

from services.agent_runner.page_server.occupant_plan import (
    Occupant,
    OccupantAction,
    OccupantVerdict,
    owning_page,
    port_of,
    reclaim_plan,
)
from services.agent_runner.page_server.page_plan import PAGE_SERVER_MODULE

OWN = "/home/own"


def test_a_page_server_command_line_names_its_port() -> None:
    assert port_of(["python", "-m", PAGE_SERVER_MODULE, "--port", "12001", "--host", "h"]) == 12001


def test_other_processes_and_portless_page_servers_hold_no_port() -> None:
    assert port_of([]) is None
    assert port_of(["python", "-m", "http.server", "--port", "12001"]) is None
    assert port_of(["python", "-m", PAGE_SERVER_MODULE, "--host", "h"]) is None


def test_the_nearest_ancestor_that_is_a_page_shell_owns_the_process() -> None:
    shells = {11: (8, "near"), 12: (7, "page")}
    assert owning_page((11, 12), shells) == (8, "near")
    assert owning_page((99, 12), shells) == (7, "page")


def test_unrelated_host_ancestry_acquires_no_ownership() -> None:
    assert owning_page((99, 1), {77: (7, "page")}) is None
    assert owning_page((), {77: (7, "page")}) is None


def test_the_plan_keeps_owned_servers_skips_foreign_homes_and_kills_the_detached() -> None:
    owned = Occupant(12010, 5010, OWN, (5009, 4001))
    wanted = Occupant(12011, 5011, OWN, (1,))
    orphan = Occupant(12012, 5012, OWN, (1,))
    foreign = Occupant(12013, 5013, "/home/another", (1,))
    unknown_home = Occupant(12014, 5014, None, (1,))

    plan = reclaim_plan(
        [owned, wanted, orphan, foreign, unknown_home], {4001: (7, "kept")}, {12010, 12011}, OWN
    )

    assert plan == (
        OccupantVerdict(owned, OccupantAction.KEEP),
        OccupantVerdict(wanted, OccupantAction.RECLAIM_WANTED),
        OccupantVerdict(orphan, OccupantAction.CLOSE_ORPHAN),
        OccupantVerdict(foreign, OccupantAction.SKIP_FOREIGN),
        OccupantVerdict(unknown_home, OccupantAction.SKIP_FOREIGN),
    )


def test_an_owned_server_of_another_home_is_still_kept() -> None:
    inside = Occupant(12010, 5010, "/home/another", (4001,))
    assert reclaim_plan([inside], {4001: (7, "kept")}, set(), OWN) == (
        OccupantVerdict(inside, OccupantAction.KEEP),
    )
