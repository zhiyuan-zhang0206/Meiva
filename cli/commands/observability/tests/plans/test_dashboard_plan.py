# meiva:note
# What converge does with the generated `meiva-ops` dashboard and its hash sidecar, and what the
# four-layer cutover check reports, decided from values alone.

from __future__ import annotations

import pytest

from cli.commands.observability.plans.lgtm.dashboard_checks import (
    GrafanaReply,
    file_problems,
    grafana_problems,
    sidecar_problems,
)
from cli.commands.observability.plans.lgtm.dashboard_plan import (
    DASHBOARD_UID,
    RENDERED_DASHBOARD_REL,
    DashboardFacts,
    InstallReference,
    KeepInstalled,
    KeepRendered,
    RefuseDashboard,
    WriteRendered,
    ghost_hash_keys,
    hash_record_update,
    orphan_removable,
    plan_dashboard_fallback,
    plan_rendered_dashboard,
)

OPS = DashboardFacts(uid=DASHBOARD_UID, has_panels=True)
ABSENT = DashboardFacts(uid="", has_panels=False)
PRE_RENAME = DashboardFacts(uid="ava-ops-main", has_panels=True)
EMPTY_OPS = DashboardFacts(uid=DASHBOARD_UID, has_panels=False)


def test_an_installed_file_equal_to_the_render_only_needs_its_hash_recorded() -> None:
    assert plan_rendered_dashboard(current="{}\n", rendered="{}\n", digest="d") == KeepRendered("d")


@pytest.mark.parametrize("current", [None, "{}\n"])
def test_a_missing_or_different_installed_file_is_replaced(current: str | None) -> None:
    assert plan_rendered_dashboard(current=current, rendered="{ }\n", digest="d") == WriteRendered(
        "d"
    )


def test_a_failed_render_keeps_the_installed_dashboard_when_it_is_the_same_dashboard() -> None:
    assert (
        plan_dashboard_fallback(installed=OPS, reference_text="ref", reference=OPS, digest="d")
        == KeepInstalled()
    )


@pytest.mark.parametrize("installed", [ABSENT, PRE_RENAME, EMPTY_OPS], ids=["none", "ava", "empty"])
def test_a_failed_render_replaces_an_installed_file_that_is_not_the_dashboard(
    installed: DashboardFacts,
) -> None:
    assert plan_dashboard_fallback(
        installed=installed, reference_text="ref", reference=OPS, digest="d"
    ) == InstallReference("d")


@pytest.mark.parametrize(
    ("reference_text", "reference"),
    [("", ABSENT), ("ref", PRE_RENAME), ("ref", EMPTY_OPS)],
    ids=["missing", "pre-rename", "no-panels"],
)
def test_a_failed_render_with_no_usable_dashboard_anywhere_refuses_to_converge(
    reference_text: str, reference: DashboardFacts
) -> None:
    assert (
        plan_dashboard_fallback(
            installed=ABSENT, reference_text=reference_text, reference=reference, digest="d"
        )
        == RefuseDashboard()
    )


def test_recording_a_digest_updates_the_sidecar_and_marks_where_it_came_from() -> None:
    assert hash_record_update({}, key=RENDERED_DASHBOARD_REL, digest="d", source="render") == {
        RENDERED_DASHBOARD_REL: "d",
        "$dashboard-source": "render",
    }


def test_recording_what_the_sidecar_already_says_changes_nothing() -> None:
    recorded = {RENDERED_DASHBOARD_REL: "d", "$dashboard-source": "render", "other": "x"}

    assert (
        hash_record_update(recorded, key=RENDERED_DASHBOARD_REL, digest="d", source="render")
        is None
    )


def test_the_same_digest_from_another_source_is_recorded_again() -> None:
    recorded = {RENDERED_DASHBOARD_REL: "d", "$dashboard-source": "render"}

    assert hash_record_update(
        recorded, key=RENDERED_DASHBOARD_REL, digest="d", source="reference"
    ) == {RENDERED_DASHBOARD_REL: "d", "$dashboard-source": "reference"}


def test_sidecar_entries_naming_no_file_are_ghosts_and_metadata_keys_are_not() -> None:
    recorded = {
        "dashboards/ava-ops-main.json": "x",
        RENDERED_DASHBOARD_REL: "d",
        "$dashboard-source": "render",
        "alerting/gone.yml": "y",
    }

    assert ghost_hash_keys(recorded, present=frozenset({RENDERED_DASHBOARD_REL})) == (
        "alerting/gone.yml",
        "dashboards/ava-ops-main.json",
    )


def test_an_orphan_is_removable_only_while_it_still_hashes_to_what_converge_wrote() -> None:
    assert orphan_removable(recorded="abc", actual="abc")
    assert not orphan_removable(recorded="abc", actual="edited")
    assert not orphan_removable(recorded=None, actual="abc")


def test_a_dashboard_is_this_dashboard_only_with_the_fixed_uid_and_panels() -> None:
    assert OPS.is_ops_dashboard()
    assert not PRE_RENAME.is_ops_dashboard()
    assert not EMPTY_OPS.is_ops_dashboard()
    assert not ABSENT.is_ops_dashboard()


def test_an_empty_document_means_the_file_is_missing_or_not_an_object() -> None:
    assert file_problems("/n/dashboard.json", {}) == (
        "dashboard file is missing or not a JSON object: /n/dashboard.json",
    )


def test_a_healthy_dashboard_file_has_no_problems() -> None:
    assert file_problems("/n/d.json", {"uid": DASHBOARD_UID, "panels": [{"id": 1}]}) == ()


def test_a_wrong_uid_and_no_panels_are_both_reported() -> None:
    assert file_problems("/n/d.json", {"uid": "ava-ops-main", "panels": []}) == (
        "dashboard uid is 'ava-ops-main', expected 'meiva-ops-main'",
        "dashboard carries no panels",
    )


def test_a_non_string_uid_reads_as_empty() -> None:
    assert file_problems("/n/d.json", {"uid": 7, "panels": [1]}) == (
        "dashboard uid is '', expected 'meiva-ops-main'",
    )


def test_a_healthy_sidecar_has_no_problems() -> None:
    assert (
        sidecar_problems("h.json", {RENDERED_DASHBOARD_REL: "d", "$dashboard-source": "render"})
        == ()
    )


def test_the_sidecar_is_checked_for_the_dashboard_the_reference_and_pre_rename_entries() -> None:
    assert sidecar_problems(
        "provisioning-hashes.json",
        {
            "$dashboard-source": "reference",
            "dashboards/ava-ops-main.json": "x",
        },
    ) == (
        f"provisioning-hashes.json does not record {RENDERED_DASHBOARD_REL}",
        "the installed dashboard is the checkout reference, not a render",
        "provisioning-hashes.json still records the pre-rename dashboards/ava-ops-main.json",
    )


GRAFANA = "http://grafana.test"
SERVED = GrafanaReply(200, {"meta": {"folderTitle": "Meiva"}})
FOLDERS = GrafanaReply(200, [{"uid": "meiva-ops", "title": "Meiva"}])


def test_a_dashboard_served_from_the_meiva_folder_with_no_old_folder_is_healthy() -> None:
    assert grafana_problems(GRAFANA, SERVED, FOLDERS) == ()


def test_the_wrong_folder_and_a_surviving_pre_rename_folder_are_reported() -> None:
    assert grafana_problems(
        GRAFANA,
        GrafanaReply(200, {"meta": {"folderTitle": "Ava"}}),
        GrafanaReply(200, [{"uid": "ava", "title": "Ava"}]),
    ) == (
        "dashboard sits in folder 'Ava', expected 'Meiva'",
        "pre-rename folder still present: 'Ava' (ava)",
    )


def test_an_unserved_dashboard_is_reported_with_the_status_grafana_gave() -> None:
    assert grafana_problems(GRAFANA, GrafanaReply(404, {}), FOLDERS) == (
        "Grafana answered 404 for dashboard uid 'meiva-ops-main'",
    )


def test_an_unreachable_grafana_is_one_problem_whatever_else_it_answered() -> None:
    assert grafana_problems(GRAFANA, GrafanaReply(0, "ConnectError: refused"), FOLDERS) == (
        "Grafana is unreachable at http://grafana.test: ConnectError: refused",
    )


def test_a_folder_listing_grafana_refuses_is_reported() -> None:
    assert grafana_problems(GRAFANA, SERVED, GrafanaReply(500, None)) == (
        "Grafana answered 500 for /api/folders",
    )


def test_a_payload_of_the_wrong_shape_reads_as_no_folder() -> None:
    assert grafana_problems(
        GRAFANA, GrafanaReply(200, "unexpected"), GrafanaReply(200, {"not": "a list"})
    ) == ("dashboard sits in folder None, expected 'Meiva'",)
