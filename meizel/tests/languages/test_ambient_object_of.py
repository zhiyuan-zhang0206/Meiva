from __future__ import annotations

import pytest

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of

POLICY = render_policy({}, {}, {}) + '\n[ambient]\nsettings = ["settings", "get_field()"]\n'
FILES = {
    "meizel.toml": '[roles]\napplication = ["app", "cfg", "base"]\ntests = ["tests"]\n',
    "meizel.policy.toml": POLICY,
    "app/__init__.py": "",
    "app/meizel.toml": "",
    "cfg/__init__.py": "settings = object()\n\n\ndef get_field(name: str) -> int:\n    return 1\n",
    "cfg/meizel.toml": '[state]\n"__init__.py" = ["settings"]\n',
    "tests/__init__.py": "",
    "base/__init__.py": "",
    "base/host/__init__.py": "",
    "base/host/boundary/__init__.py": "",
    "base/host/boundary/untyped.py": (
        "def object_of(function, /, *args, **kwargs) -> object:\n"
        "    return function(*args, **kwargs)\n"
    ),
}
HEADER = "from base.host.boundary.untyped import object_of\nimport cfg\nfrom cfg import get_field\n"


# meiva:note
# The (kind, target) pairs `app/mod.py` acquires, whatever the rule judges.
def acquired(make_repo: RepoFactory, body: str) -> list[tuple[str, str]]:
    root = make_repo({**FILES, "app/mod.py": f"{HEADER}\nreaders = [get_field]\n\n{body}\n"})
    return [(a.kind, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient]


@pytest.mark.parametrize(
    "call",
    [
        "object_of(get_field, 'x')",
        "object_of(get_field, name='x')",
        "object_of(cfg.get_field, 'x')",
    ],
)
def test_a_reader_handed_to_object_of_is_acquired_like_the_call_of_the_reader(
    make_repo: RepoFactory, call: str
) -> None:
    assert acquired(make_repo, f"x = {call}") == [("settings", "cfg.get_field")]


def test_a_function_that_is_not_a_reader_handed_to_object_of_is_not_acquired(
    make_repo: RepoFactory,
) -> None:
    assert acquired(make_repo, "import json\n\nx = object_of(json.loads, '1')") == []


@pytest.mark.parametrize(
    "call",
    [
        "object_of(lambda name: name, 'x')",
        "object_of(readers[0], 'x')",
        "object_of(*readers)",
    ],
)
def test_object_of_is_not_read_as_a_reader_call_when_its_first_argument_is_not_a_name(
    make_repo: RepoFactory, call: str
) -> None:
    assert acquired(make_repo, f"x = {call}") == []


def test_a_reader_handed_to_another_function_is_not_acquired(make_repo: RepoFactory) -> None:
    assert acquired(make_repo, "def apply(function, *args): ...\n\napply(get_field, 'x')") == []
