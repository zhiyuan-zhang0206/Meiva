from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of

LIB = {
    "lib/__init__.py": "",
    "lib/mod.py": "def helper() -> int:\n    return 1\n",
    "app/__init__.py": "",
}


def raising_false(make_repo: RepoFactory, source: str) -> list[tuple[str, str]]:
    root = make_repo({**LIB, "app/test_x.py": source})
    return sorted(
        (d.code.value, d.debt.key)
        for d in analysis_of(root).diagnostics
        if d.debt is not None
        and d.path == "app/test_x.py"
        and d.code.value == "patch-raising-false"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None, raising=False)\n",
            [("patch-raising-false", "test_x.py#monkeypatch.setattr lib.mod.helper")],
        ),
        (
            "import time\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(time, 'sleep', None, raising=False)\n",
            [("patch-raising-false", "test_x.py#monkeypatch.setattr time.sleep")],
        ),
        (
            "def test_a(monkeypatch, thing):\n    monkeypatch.setattr(thing, 'x', 1, raising=False)\n",
            [("patch-raising-false", "test_x.py#monkeypatch.setattr thing.x")],
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.setattr('time.sleep', None, raising=False)\n",
            [("patch-raising-false", "test_x.py#monkeypatch.setattr time.sleep")],
        ),
        (
            "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None, raising=False)\n    monkeypatch.setattr(mod, 'helper', None, raising=False)\n",
            [
                ("patch-raising-false", "test_x.py#monkeypatch.setattr lib.mod.helper"),
                ("patch-raising-false", "test_x.py#monkeypatch.setattr lib.mod.helper"),
            ],
        ),
    ],
)
def test_a_patch_that_tolerates_a_missing_target_is_recorded_by_form_and_target(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert raising_false(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None)\n",
        "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None, raising=True)\n",
        "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.delattr(mod, 'helper', raising=False)\n",
        "import os\n\ndef test_a(monkeypatch):\n    monkeypatch.delattr(os, 'pidfd_open', raising=False)\n",
        "def test_a(monkeypatch):\n    monkeypatch.delenv('A', raising=False)\n",
        "import os\n\ndef test_a(monkeypatch):\n    monkeypatch.delitem(os.environ, 'A', raising=False)\n",
    ],
)
def test_other_patches_and_other_methods_are_not_this_code(
    make_repo: RepoFactory, source: str
) -> None:
    assert raising_false(make_repo, source) == []


def test_optional_attribute_deletion_still_counts_the_actual_patch(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            **LIB,
            "app/test_x.py": (
                "import os\n\ndef test_a(monkeypatch):\n"
                "    monkeypatch.delattr(os, 'pidfd_open', raising=False)\n"
            ),
        }
    )
    assert [
        d.code.value
        for d in analysis_of(root).diagnostics
        if d.path == "app/test_x.py" and d.debt is not None
    ] == ["stdlib-patch"]
