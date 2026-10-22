from __future__ import annotations

from base.host.permissions_helper.grants import catalog
from base.host.permissions_helper.grants.catalog import Grant, PermissionItem, RequestMode


def test_ids_are_unique_and_indexed() -> None:
    ids = [item.id for item in catalog.CATALOG]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(catalog.ITEMS_BY_ID)


def test_every_item_states_why_where_and_how_to_verify() -> None:
    for item in catalog.CATALOG:
        assert isinstance(item, PermissionItem)
        assert item.why and item.why.endswith("."), item.id
        assert item.verify, item.id
        assert item.pane.startswith("Privacy_"), item.id
        assert catalog.settings_url(item).endswith(item.pane)


def test_every_row_is_probed_and_the_desktop_rows_also_read_the_helper() -> None:
    for item in catalog.CATALOG:
        assert item.services, item.id
        for service in item.services:
            assert service.startswith("kTCCService"), (item.id, service)
            assert service in catalog.PREFLIGHT_SERVICES
        if item.ping_key:
            assert item.ping_key in catalog.PING_KEYS, item.id
            assert item.helper_restart, item.id


def test_required_set_is_the_product_desktop_surface() -> None:
    required = {item.id for item in catalog.REQUIRED_ITEMS}
    assert {
        "screen-recording",
        "accessibility",
        "app-management",
        "local-network",
        "desktop-folder",
        "documents-folder",
        "downloads-folder",
        *(f"automation-{suffix}" for suffix, _ in catalog.AUTOMATION_TARGETS),
    } == required
    assert all(not item.required for item in catalog.REPORT_ONLY_ITEMS)


# meiva:note
# App Management is the one row no dialog can grant: tccd refuses to prompt this service
# for an unentitled client ("Service kTCCServiceSystemPolicyAppBundles does not allow
# prompting for unentitled binaries; returning denied") and records that denial, so the
# row must stay SYSTEM_SETTINGS and must not grow a request or a helper boolean (#5481,
# measured 2026-10-21).
def test_app_management_sends_the_person_to_its_pane_instead_of_asking() -> None:
    item = catalog.ITEMS_BY_ID["app-management"]
    assert item.mode is RequestMode.SYSTEM_SETTINGS
    assert item.required
    assert item.group == "desktop"
    assert item.services == ("kTCCServiceSystemPolicyAppBundles",)
    assert item.pane == "Privacy_AppBundles"
    assert catalog.settings_url(item).endswith("?Privacy_AppBundles")
    assert "App Management" in item.verify
    assert "no longer fails" in item.verify
    assert "by hand" in item.verify
    assert not item.ping_key


# meiva:note
# Local network is the row macOS writes on the first real packet, not on a request: the
# helper has no capability that could report it, and the catalog must still list it so a
# missing grant is a named row instead of an unexplained cross-machine failure (#5541).
def test_local_network_is_listed_and_reported_but_never_read() -> None:
    item = catalog.ITEMS_BY_ID["local-network"]
    assert item.mode is RequestMode.LIVE_EVENT
    assert item.required
    assert item.group == "desktop"
    assert item.services == ("kTCCServiceLocalNetwork",)
    assert item.pane == "Privacy_LocalNetwork"
    assert not item.ping_key
    assert "Local Network" in item.verify


def test_report_only_rows_are_never_prompted() -> None:
    for item in catalog.REPORT_ONLY_ITEMS:
        assert item.mode is RequestMode.REPORT_ONLY
        assert not item.required


def test_folder_items_declare_the_folder_their_request_touches() -> None:
    for item in catalog.FOLDER_ITEMS:
        assert item.mode is RequestMode.PROMPT
        assert item.home_subpath in ("Desktop", "Documents", "Downloads")


def test_grant_vocabulary_is_fail_fast() -> None:
    assert {member.value for member in Grant} == {
        "granted",
        "denied",
        "not_determined",
        "stale",
        "unknown",
    }
