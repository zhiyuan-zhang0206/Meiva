from services.desktop.browser.tab_groups.page_selection import (
    listed_page_of_target,
    target_of_listed_page,
)

LISTING = {1: "https://a/", 2: "https://b/", 3: "https://c/"}
PAGES = {"T1": "https://a/", "T2": "https://b/", "T9": "https://z/"}


def test_a_page_and_its_target_are_matched_by_a_url_only_one_tab_shows() -> None:
    assert target_of_listed_page(LISTING, PAGES, 2) == "T2"
    assert listed_page_of_target(LISTING, PAGES, "T2") == 2


def test_a_page_or_target_the_other_side_does_not_have_matches_nothing() -> None:
    assert target_of_listed_page(LISTING, PAGES, 3) is None
    assert target_of_listed_page(LISTING, PAGES, 7) is None
    assert listed_page_of_target(LISTING, PAGES, "T9") is None
    assert listed_page_of_target(LISTING, PAGES, "T404") is None


def test_two_tabs_on_one_url_on_either_side_match_nothing() -> None:
    twin_pages = {**PAGES, "T3": "https://a/"}
    twin_listing = {**LISTING, 4: "https://a/"}
    assert target_of_listed_page(LISTING, twin_pages, 1) is None
    assert listed_page_of_target(LISTING, twin_pages, "T1") is None
    assert target_of_listed_page(twin_listing, PAGES, 1) is None
    assert listed_page_of_target(twin_listing, PAGES, "T1") is None
