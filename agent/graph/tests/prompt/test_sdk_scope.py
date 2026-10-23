# meiva:note
# The expansion plan: which SDK paths the system prompt expands, decided from values.
from __future__ import annotations

import pytest

from agent.graph.prompt.sdk_scope import (
    ExpandPlan,
    disabled_by_sdk_config,
    expand_plan,
    is_capability_surface_member,
)

DISCOVERED = ["agents", "files", "self", "shell", "shell.sessions"]


def test_a_list_without_the_wildcard_is_kept_in_order() -> None:
    plan = expand_plan(["files", "shell", "self"], plugin_expansions=(), discovered=DISCOVERED)

    assert plan == ExpandPlan(("files", "shell", "self"), ())


def test_the_wildcard_is_replaced_by_the_discovered_namespaces() -> None:
    plan = expand_plan(["*"], plugin_expansions=(), discovered=DISCOVERED)

    assert plan.paths == tuple(DISCOVERED)


def test_plugin_expansions_lead_and_repeats_keep_their_first_place() -> None:
    plan = expand_plan(
        ["files", "*", "cwd"], plugin_expansions=("cwd", "tasks"), discovered=DISCOVERED
    )

    assert plan.paths == ("cwd", "tasks", "files", "agents", "self", "shell", "shell.sessions")


def test_members_of_a_capability_surface_are_refused_and_reported() -> None:
    plan = expand_plan(
        ["skills", "skills.gmail", "files", "mcps.chrome", "skills.gmail"],
        plugin_expansions=(),
        discovered=(),
    )

    assert plan.paths == ("skills", "files")
    assert plan.refused == ("skills.gmail", "mcps.chrome")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("skills.gmail", True),
        ("mcps.chrome", True),
        ("skills", False),
        ("mcps", False),
        ("shell.skills", False),
        ("files", False),
    ],
)
def test_only_members_below_a_capability_surface_count(path: str, expected: bool) -> None:
    assert is_capability_surface_member(path) is expected


def test_a_disable_entry_covers_the_path_and_everything_below_it() -> None:
    disabled = ("watcher", "self.terminate")

    assert disabled_by_sdk_config("watcher", disabled)
    assert disabled_by_sdk_config("watcher.launch", disabled)
    assert disabled_by_sdk_config("self.terminate", disabled)
    assert not disabled_by_sdk_config("self", disabled)
    assert not disabled_by_sdk_config("watchers", disabled)
