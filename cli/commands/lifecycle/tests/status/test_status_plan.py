# meiva:note
# What `meiva status` prints and in which order, decided from values alone.
#
# `plan_status` returns the steps the shell executes, so the order and presence of every
# section is one equality on the returned tuple. `service_table` decides the note each
# service row carries.

from __future__ import annotations

from cli.commands.lifecycle.status.plan import (
    PrintLines,
    PrintServiceRows,
    ProbeDataPlane,
    ProbeLgtm,
    ProbeRedisBridge,
    ReadGateway,
    ReadHostResources,
    ReadRelease,
    ReadSourceDrift,
    ReportConverge,
    StatusFacts,
    plan_status,
)
from cli.commands.lifecycle.status.roster import (
    NOT_LAUNCHED_NOTE,
    RosterEntry,
    ServiceTable,
    service_table,
)

_GATEWAY = RosterEntry("gateway", "meiva-gateway", None)
_FRONTEND = RosterEntry("frontend", "meiva-frontend", None)
_BROWSER = RosterEntry("browser", "meiva-browser", "no display (headless server)")


def _facts(
    roles: frozenset[str] | None,
    *,
    roster: tuple[RosterEntry, ...] = (_GATEWAY,),
    launched: frozenset[str] | None = frozenset({"gateway"}),
    lgtm_host: bool = False,
) -> StatusFacts:
    return StatusFacts(
        repo="/repo", roles=roles, roster=roster, launched=launched, lgtm_host=lgtm_host
    )


def _kinds(steps: tuple[object, ...]) -> list[str]:
    return [type(step).__name__ for step in steps]


def test_a_gateway_host_prints_every_section_in_order() -> None:
    plan = plan_status(_facts(frozenset({"gateway"})))

    assert plan == (
        PrintLines(("[meiva status] cwd = /repo  roles = gateway", "")),
        ReportConverge(),
        PrintLines(("",)),
        PrintServiceRows(
            ServiceTable(
                header="service        root  probe",
                rule="-" * len("service        root  probe"),
                name_width=13,
                notes=(None,),
            )
        ),
        PrintLines(("", "infra (pg/redis):")),
        ProbeDataPlane(),
        PrintLines(("", "host (live cpu/memory/disk):")),
        ReadHostResources(),
        PrintLines(("", "redis bridge (private-network ingress):")),
        ProbeRedisBridge(),
        ReadSourceDrift(),
        PrintLines(("",)),
        ReadRelease("/repo"),
        ReadGateway(),
    )


def test_a_pure_agent_runner_has_no_local_data_plane_and_no_relay_to_report() -> None:
    plan = plan_status(_facts(frozenset({"agent-runner"})))

    assert PrintLines(("", "infra (pg/redis): skipped (agent-runner uses central node)")) in plan
    assert ProbeDataPlane() not in plan
    assert ProbeRedisBridge() not in plan
    assert PrintLines(("", "redis bridge (private-network ingress):")) not in plan
    # meiva:note
    # The host reading, the drift check and the release still show.
    assert ReadHostResources() in plan
    assert ReadSourceDrift() in plan
    assert ReadRelease("/repo") in plan


def test_a_single_box_serving_gateway_and_agent_runner_keeps_the_data_plane() -> None:
    plan = plan_status(_facts(frozenset({"gateway", "agent-runner"})))

    assert ProbeDataPlane() in plan
    assert ProbeRedisBridge() in plan


def test_a_station_only_host_is_not_a_runner_only_host() -> None:
    plan = plan_status(_facts(frozenset({"observability-station"})))

    assert ProbeDataPlane() in plan
    assert ProbeRedisBridge() in plan


def test_an_unresolved_role_shows_the_data_plane_but_skips_the_drift_check() -> None:
    plan = plan_status(_facts(None))

    assert plan[0] == PrintLines(("[meiva status] cwd = /repo  roles = unknown", ""))
    assert ProbeDataPlane() in plan
    assert ProbeRedisBridge() in plan
    assert ReadSourceDrift() not in plan


def test_the_lgtm_section_shows_only_on_the_designated_host() -> None:
    on = plan_status(_facts(frozenset({"gateway"}), lgtm_host=True))
    off = plan_status(_facts(frozenset({"gateway"}), lgtm_host=False))

    assert ProbeLgtm() in on
    assert ProbeLgtm() not in off
    # meiva:note
    # It sits between the host reading and the relay.
    assert _kinds(on).index("ProbeLgtm") > _kinds(on).index("ReadHostResources")
    assert _kinds(on).index("ProbeLgtm") < _kinds(on).index("ProbeRedisBridge")


def test_the_lgtm_section_shows_on_a_pure_agent_runner_when_it_is_the_designated_host() -> None:
    plan = plan_status(_facts(frozenset({"agent-runner"}), lgtm_host=True))

    assert ProbeLgtm() in plan
    assert ProbeDataPlane() not in plan


def test_the_gateway_view_closes_the_report_after_the_local_probes() -> None:
    plan = plan_status(_facts(frozenset({"gateway"}), lgtm_host=True))

    assert _kinds(plan)[-3:] == ["PrintLines", "ReadRelease", "ReadGateway"]
    assert _kinds(plan).index("ProbeDataPlane") < _kinds(plan).index("ReadGateway")


# meiva:note
# ─── the service table ────────────────────────────────────────────────────────


def test_a_kept_service_the_last_start_launched_carries_no_note() -> None:
    table = service_table((_GATEWAY,), frozenset({"gateway"}))

    assert table.notes == (None,)


def test_a_kept_service_the_last_start_did_not_launch_says_so() -> None:
    table = service_table((_GATEWAY, _FRONTEND), frozenset({"gateway"}))

    assert table.notes == (None, NOT_LAUNCHED_NOTE)
    assert NOT_LAUNCHED_NOTE == "not launched by the last meiva start"


def test_before_any_start_nothing_was_launched() -> None:
    table = service_table((_GATEWAY, _FRONTEND), None)

    assert table.notes == (NOT_LAUNCHED_NOTE, NOT_LAUNCHED_NOTE)


def test_a_gate_reason_wins_over_the_launch_record() -> None:
    # meiva:note
    # A still-launched but now-gated service reads as its gate, not as launched.
    table = service_table((_BROWSER,), frozenset({"browser"}))
    unlaunched = service_table((_BROWSER,), frozenset())

    assert table.notes == ("no display (headless server)",)
    assert unlaunched.notes == ("no display (headless server)",)


def test_the_first_column_is_as_wide_as_the_longest_name() -> None:
    table = service_table((_GATEWAY, _FRONTEND, _BROWSER), None)

    assert table.name_width == len("meiva-frontend")
    assert table.header == "service         root  probe"
    assert table.rule == "-" * len(table.header)
    assert len(table.notes) == 3


def test_an_empty_roster_still_gets_a_heading() -> None:
    table = service_table((), None)

    assert table == ServiceTable(
        header="service  root  probe", rule="-" * 20, name_width=7, notes=()
    )
