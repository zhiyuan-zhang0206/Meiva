from __future__ import annotations

from pathlib import Path

import pytest

from meizel.allowances import parse
from meizel.graph.model import Diagnostic
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, git, run

LIB = {"lib/__init__.py": "", "lib/mod.py": "def helper() -> int:\n    return 1\n"}


def codes(make_repo: RepoFactory, source: str) -> list[tuple[str, str]]:
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    return sorted(
        (d.code.value, d.debt.key)
        for d in analysis_of(root).diagnostics
        if d.debt is not None and d.path == "app/test_x.py" and d.code.value.endswith("patch")
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import time\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(time, 'sleep', None)\n",
            ("stdlib-patch", "test_x.py#monkeypatch.setattr time.sleep"),
        ),
        (
            "import json\nfrom unittest import mock\n\ndef test_a():\n    with mock.patch.object(json, 'dumps'):\n        pass\n",
            ("stdlib-patch", "test_x.py#patch.object json.dumps"),
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch('time.sleep'):\n        pass\n",
            ("stdlib-patch", "test_x.py#patch time.sleep"),
        ),
        (
            "import requests\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(requests, 'get', None)\n",
            ("third-party-patch", "test_x.py#monkeypatch.setattr requests.get"),
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch('requests.get'):\n        pass\n",
            ("third-party-patch", "test_x.py#patch requests.get"),
        ),
        (
            "import os\nfrom unittest import mock\n\ndef test_a():\n    with mock.patch.dict(os.environ, {'A': '1'}):\n        pass\n",
            ("env-patch", "test_x.py#patch.dict os.environ"),
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch.dict('os.environ', {'A': '1'}):\n        pass\n",
            ("env-patch", "test_x.py#patch.dict os.environ"),
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.setenv('A', '1')\n",
            ("env-patch", "test_x.py#monkeypatch.setenv os.environ.A"),
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.delenv('A', raising=False)\n",
            ("env-patch", "test_x.py#monkeypatch.delenv os.environ.A"),
        ),
        (
            "import os\n\ndef test_a(monkeypatch):\n    monkeypatch.setitem(os.environ, 'A', '1')\n",
            ("env-patch", "test_x.py#monkeypatch.setitem os.environ.A"),
        ),
        (
            "def test_a(monkeypatch, thing):\n    monkeypatch.setattr(thing, 'x', 1)\n",
            ("unresolved-patch", "test_x.py#monkeypatch.setattr thing.x"),
        ),
        (
            "class Local:\n    def go(self): ...\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(Local, 'go', None)\n",
            ("unresolved-patch", "test_x.py#monkeypatch.setattr Local.go"),
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.setattr(make().inner, 'x', 1)\n\ndef make(): ...\n",
            ("unresolved-patch", "test_x.py#monkeypatch.setattr make().inner.x"),
        ),
        (
            "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None)\n",
            ("first-party-patch", "test_x.py#monkeypatch.setattr lib.mod.helper"),
        ),
    ],
)
def test_each_kind_of_patch_has_its_own_code(
    make_repo: RepoFactory, source: str, expected: tuple[str, str]
) -> None:
    assert codes(make_repo, source) == [expected]


def test_a_first_party_string_target_stays_restricted_python(make_repo: RepoFactory) -> None:
    source = "from unittest import mock\n\ndef test_a():\n    with mock.patch('lib.mod.helper'):\n        pass\n"
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert [d for d in analysis_of(root).diagnostics if d.code.value.endswith("patch")] == []
    assert len(diagnostics(root, "restricted-python")) == 1


def test_an_unreadable_string_target_stays_restricted_python(make_repo: RepoFactory) -> None:
    source = (
        "from unittest import mock\n\ndef test_a(name):\n    with mock.patch(name):\n        pass\n"
    )
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert len(diagnostics(root, "restricted-python")) == 1


def test_every_patch_code_is_recorded_like_any_other_debt(make_repo: RepoFactory) -> None:
    source = (
        "import time\n\ndef test_a(monkeypatch, thing):\n"
        "    monkeypatch.setattr(time, 'sleep', None)\n"
        "    monkeypatch.setattr(thing, 'x', 1)\n"
        "    monkeypatch.setenv('A', '1')\n"
    )
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert run(root, "debt", "record", "app") == 0
    git(root, "add", "-A")
    text = (root / "app/meizel.toml").read_text()
    assert "[debt.env-patch]" in text
    assert "[debt.stdlib-patch]" in text
    assert "[debt.unresolved-patch]" in text
    assert [d for d in analysis_of(root).verdict if d.code.value.endswith("patch")] == []


def test_the_messages_say_what_to_do_instead(make_repo: RepoFactory) -> None:
    source = "import time\n\ndef test_a(monkeypatch, thing):\n    monkeypatch.setattr(time, 'sleep', None)\n    monkeypatch.setattr(thing, 'x', 1)\n    monkeypatch.setenv('A', '1')\n"
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    texts = {
        d.code.value: d.render()
        for d in analysis_of(root).diagnostics
        if d.code.value.endswith("patch")
    }
    assert "explicit parameter" in texts["stdlib-patch"]
    assert "read the setting once at the boundary" in texts["env-patch"]
    assert "not an imported module or object" in texts["unresolved-patch"]


def test_a_new_unresolved_patch_in_a_strict_to_be_directory_fails(make_repo: RepoFactory) -> None:
    source = "def test_a(monkeypatch, thing):\n    monkeypatch.setattr(thing, 'x', 1)\n    monkeypatch.setattr(thing, 'y', 1)\n"
    baseline = '[debt.unresolved-patch]\n"test_x.py#monkeypatch.setattr thing.x" = 1\n'
    root = make_repo(
        {**LIB, "app/__init__.py": "", "app/test_x.py": source, "app/meizel.toml": baseline}
    )
    [found] = [d for d in analysis_of(root).verdict if d.code.value == "unresolved-patch"]
    assert "thing.y" in found.message
    assert "app is strict-to-be" in found.render()


GUARD = (
    "import lib.mod\n\n"
    "def guard(monkeypatch):\n"
    "    monkeypatch.setattr(lib.mod, 'helper', lambda: 2)\n"
)
APPROVAL_REPO = {
    "lib/__init__.py": "",
    "lib/mod.py": "def helper() -> int:\n    return 1\n",
    "app/__init__.py": "",
    "app/tests/__init__.py": "",
    "app/tests/guards.py": GUARD,
    "app/tests/other.py": GUARD,
}


def approving(*paths: str, reason: str = "keeps the suite off the host") -> str:
    entries = "".join(f'"{path}" = "{reason}"\n' for path in paths)
    return f"[approved-patches.files]\n{entries}"


def approval_judged(
    make_repo: RepoFactory, policy: str, extra: dict[str, str] | None = None
) -> tuple[Path, list[Diagnostic]]:
    root = make_repo({**APPROVAL_REPO, **(extra or {}), "meizel.policy.toml": policy})
    return root, analysis_of(root).diagnostics


def patches_of(found: list[Diagnostic], path: str) -> list[str]:
    return [d.code.value for d in found if d.path == path and d.code.value.endswith("-patch")]


def test_a_file_the_policy_approves_records_no_patch_and_its_neighbours_still_do(
    make_repo: RepoFactory,
) -> None:
    _, found = approval_judged(make_repo, approving("app/tests/guards.py"))
    assert patches_of(found, "app/tests/guards.py") == []
    assert patches_of(found, "app/tests/other.py") == ["first-party-patch"]


def test_without_the_approval_the_same_file_is_debt(make_repo: RepoFactory) -> None:
    _, found = approval_judged(make_repo, "")
    assert patches_of(found, "app/tests/guards.py") == ["first-party-patch"]


def test_an_approved_file_may_not_patch_a_name_that_does_not_exist(make_repo: RepoFactory) -> None:
    source = (
        "import lib.mod\n\ndef guard(monkeypatch):\n    monkeypatch.setattr(lib.mod, 'gone', 1)\n"
    )
    _, found = approval_judged(
        make_repo, approving("app/tests/guards.py"), {"app/tests/guards.py": source}
    )
    assert [d.code.value for d in found if d.path == "app/tests/guards.py"].count(
        "missing-patch-target"
    ) == 1


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("lib/mod.py", "production code"),
        ("app/tests/test_guards.py", "a test module"),
        ("app/tests/guards_test.py", "a test module"),
    ],
)
def test_production_code_and_test_modules_cannot_be_approved(
    make_repo: RepoFactory, path: str, why: str
) -> None:
    extra = {path: GUARD} if path.startswith("app/tests/") else {}
    _, found = approval_judged(make_repo, approving(path), extra)
    [refused] = [d for d in found if d.code.value == "policy-syntax"]
    assert path in refused.message
    assert why in refused.message


def test_an_approval_of_a_file_that_patches_nothing_is_stale(make_repo: RepoFactory) -> None:
    quiet = "def guard() -> None:\n    return None\n"
    _, found = approval_judged(
        make_repo, approving("app/tests/quiet.py"), {"app/tests/quiet.py": quiet}
    )
    [stale] = [d for d in found if d.code.value == "stale-approval"]
    assert "patches nothing any more" in stale.message
    assert stale.line == 2


def test_an_approval_of_a_file_that_is_gone_is_stale(make_repo: RepoFactory) -> None:
    _, found = approval_judged(make_repo, approving("app/tests/gone.py"))
    [stale] = [d for d in found if d.code.value == "stale-approval"]
    assert "names no tracked file" in stale.message


def test_an_approval_that_still_approves_is_not_stale(make_repo: RepoFactory) -> None:
    root, _ = approval_judged(make_repo, approving("app/tests/guards.py"))
    assert diagnostics(root, "stale-approval") == []


def test_an_approval_without_a_reason_is_refused() -> None:
    _, problems = parse('[approved-patches.files]\n"app/tests/guards.py" = " "\n')
    assert problems == [
        "[approved-patches.files] 'app/tests/guards.py' needs a non-empty reason string"
    ]


VITEST_SETUP = 'import { vi } from "vitest";\n\nvi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));\n'
VITEST_REPO = {"ui/tests/setup.ts": VITEST_SETUP, "meizel.toml": '[roles]\nfrontend = ["ui"]\n'}


@pytest.mark.parametrize(
    ("policy", "expected"),
    [("", {"third-party-patch"}), (approving("ui/tests/setup.ts"), set[str]())],
)
def test_a_typescript_setup_file_is_approved_like_a_python_one(
    make_repo: RepoFactory, policy: str, expected: set[str]
) -> None:
    root = make_repo({**VITEST_REPO, "meizel.policy.toml": policy})
    found = analysis_of(root).diagnostics
    assert {d.code.value for d in found if d.path == "ui/tests/setup.ts"} == expected


def test_a_typescript_test_module_cannot_be_approved(make_repo: RepoFactory) -> None:
    files = {**VITEST_REPO, "ui/tests/view.test.ts": VITEST_SETUP}
    root = make_repo({**files, "meizel.policy.toml": approving("ui/tests/view.test.ts")})
    [refused] = [d for d in analysis_of(root).diagnostics if d.code.value == "policy-syntax"]
    assert "a test module" in refused.message


def test_the_listing_counts_the_patches_of_each_approved_file(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**APPROVAL_REPO, "meizel.policy.toml": approving("app/tests/guards.py")})
    assert run(root, "debt", "approved") == 0
    out = capsys.readouterr().out
    assert "app/tests/guards.py: 1 site -- keeps the suite off the host" in out
    assert "1 patch site in 1 approved file, not recorded as debt" in out
