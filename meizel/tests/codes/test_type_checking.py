from __future__ import annotations

from pathlib import Path

import pytest

from meizel import allowances
from meizel.contracts.analysis import analyze, diagnose, load_policy
from meizel.contracts.cli import GitRepo
from meizel.graph.model import Code, FactKind, Sources
from meizel.graph.universe import Universe
from meizel.language.scan import scan_source
from meizel.tests.synthetic import STANDALONE_PROJECT, RepoFactory, run, seat, write

GUARD = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import decimal\n"
HOSTS = {
    **{f"{d}/__init__.py": "" for d in ("pkg", "tools", "jobs", "tests")},
    "runtimes/embed/pyproject.toml": STANDALONE_PROJECT.replace(
        "meiva_embedding_service", "embedded"
    ),
    "runtimes/embed/uv.lock": "",
    "runtimes/embed/tests/test_embed.py": "",
    "runtimes/embed/src/embedded/__init__.py": "",
}
ROLES = '[roles]\napplication = ["pkg"]\nstandalone = ["runtimes"]\ntooling = ["tools"]\ntemplates = ["jobs"]\ntests = ["tests"]\n'


@pytest.mark.parametrize(
    "source",
    [
        GUARD,
        "import typing\nif typing.TYPE_CHECKING:\n    pass",
        "import typing as t\nif t.TYPE_CHECKING:\n    pass",
        "from typing import TYPE_CHECKING as TC\nif TC:\n    pass",
        "from typing import TYPE_CHECKING as TC\ndef f():\n    if TC:\n        pass",
        "import typing as t\nclass C:\n    t = object()\n    def f(self):\n        if t.TYPE_CHECKING:\n            pass",
    ],
)
def test_import_identity_records_direct_guards(tmp_path: Path, source: str) -> None:
    scan = scan_source(
        Universe(GitRepo(tmp_path), ["pkg/a.py"]),
        "pkg/a.py",
        source.encode(),
        Sources(),
        policy=allowances.Policy(),
    )
    assert len(scan.type_checking) == 1


@pytest.mark.parametrize(
    "source",
    [
        "text = 'if TYPE_CHECKING: import example'\n# if TYPE_CHECKING:",
        "TYPE_CHECKING = False\nif TYPE_CHECKING:\n    pass",
        "import other\nif other.TYPE_CHECKING:\n    pass",
        "import typing as t\ndef f(t):\n    if t.TYPE_CHECKING:\n        pass",
        "from typing import TYPE_CHECKING as TC\nTC = False\nif TC:\n    pass",
        "import typing as t\ntry: pass\nexcept Exception as t:\n    if t.TYPE_CHECKING:\n        pass",
        "import typing as t\nwith manager as t:\n    if t.TYPE_CHECKING:\n        pass",
        "import typing as t\nclass C[t]:\n    if t.TYPE_CHECKING:\n        pass",
        "import typing as t\ndef f[t]():\n    if t.TYPE_CHECKING:\n        pass",
        "import typing as t\nif not t.TYPE_CHECKING:\n    pass",
    ],
)
def test_text_other_objects_and_shadowed_bindings_are_not_guards(
    tmp_path: Path, source: str
) -> None:
    scan = scan_source(
        Universe(GitRepo(tmp_path), ["pkg/a.py"]),
        "pkg/a.py",
        source.encode(),
        Sources(),
        policy=allowances.Policy(),
    )
    assert scan.type_checking == ()


@pytest.mark.parametrize(
    "path",
    [
        "pkg/a.py",
        "runtimes/embed/src/embedded/helper.py",
        "tools/a.py",
        "jobs/a.py",
        "tests/helpers.py",
        "pkg/tests/helpers.py",
        "pkg/tests/conftest.py",
        "conftest.py",
    ],
)
def test_current_repository_roles_and_fixture_entries_are_governed(
    make_repo: RepoFactory, path: str
) -> None:
    root = make_repo({**HOSTS, "meizel.toml": ROLES, path: GUARD})
    full = analyze(seat(root), jobs=1)
    changed = analyze(seat(root), jobs=1, changed=[path])
    sites = [d for d in full.diagnostics if d.code is Code.TYPE_CHECKING]
    assert [(d.path, d.line, d.debt) for d in sites] == [(path, 2, None)]
    assert sites == [d for d in changed.diagnostics if d.code is Code.TYPE_CHECKING]
    policy, problems = load_policy(full.universe)
    assert not problems
    assert sites == [
        d
        for d in diagnose(full.scans[path], full.manifests, policy=policy)
        if d.code is Code.TYPE_CHECKING
    ]


@pytest.mark.parametrize(
    "path", ["pkg/tests/test_a.py", "tests/test_a.py", "demos/a.py", "ui/a.py"]
)
def test_direct_test_modules_and_other_roles_are_not_expanded(
    make_repo: RepoFactory, path: str
) -> None:
    root = make_repo({**HOSTS, "meizel.toml": ROLES, path: GUARD})
    assert not [d for d in analyze(seat(root), jobs=1).diagnostics if d.code is Code.TYPE_CHECKING]


def test_checker_repository_roles_are_not_the_target_roles(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "meizel.toml": '[roles]\napplication = ["local"]\n',
            "local/a.py": GUARD,
            "base/a.py": GUARD,
        }
    )
    assert [
        d.path for d in analyze(seat(root), jobs=1).diagnostics if d.code is Code.TYPE_CHECKING
    ] == ["local/a.py"]


@pytest.mark.parametrize("allowed", [True, False])
def test_policy_is_loaded_from_the_analyzed_repository(
    make_repo: RepoFactory, allowed: bool
) -> None:
    policy = '[type-checking.files]\n"agent/graph/__init__.py" = "A local reviewed exception"\n'
    root = make_repo(
        {
            "meizel.toml": '[roles]\napplication = ["agent"]\n',
            "agent/graph/__init__.py": GUARD,
            "meizel.policy.toml": policy if allowed else "",
        }
    )
    found = analyze(seat(root), jobs=1)
    sites = [d for d in found.diagnostics if d.code is Code.TYPE_CHECKING]
    assert bool(sites) is not allowed
    current, problems = load_policy(found.universe)
    assert not problems
    assert sites == [
        d
        for d in diagnose(found.scans["agent/graph/__init__.py"], found.manifests, policy=current)
        if d.code is Code.TYPE_CHECKING
    ]


@pytest.mark.parametrize(
    "purity",
    ['{ mode = "none", why = "not judged" }', '"strict-to-be"', '"strict"'],
)
def test_the_guard_is_hard_and_cannot_be_recorded(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str], purity: str
) -> None:
    root = make_repo(
        {**HOSTS, "meizel.toml": ROLES + f'\n[purity]\n"." = {purity}\n', "pkg/a.py": GUARD}
    )
    assert any(d.code is Code.TYPE_CHECKING for d in analyze(seat(root), jobs=1).verdict)
    assert run(root, "debt", "record", ".") == 1
    assert "cannot be recorded" in capsys.readouterr().err
    assert "debt.type-checking" not in (root / "meizel.toml").read_text()


def test_guard_records_do_not_skip_body_or_else_facts(tmp_path: Path) -> None:
    source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pkg.a\nelse:\n    import pkg.b\n"
    scan = scan_source(
        Universe(GitRepo(tmp_path), ["pkg/a.py", "pkg/b.py", "entry.py"]),
        "entry.py",
        source.encode(),
        Sources(),
        policy=allowances.Policy(),
    )
    assert scan.type_checking == (2,)
    assert {f.dst for f in scan.facts if f.kind is FactKind.IMPORT} == {"pkg/a.py", "pkg/b.py"}


def test_changed_guard_diagnostics_follow_add_remove_and_rename(make_repo: RepoFactory) -> None:
    root = make_repo({**HOSTS, "meizel.toml": ROLES, "tools/a.py": GUARD})
    write(root, {"tools/a.py": "", "tools/b.py": GUARD})
    changed = analyze(seat(root), jobs=1, changed=["tools/a.py", "tools/b.py"])
    assert [d.path for d in changed.diagnostics if d.code is Code.TYPE_CHECKING] == ["tools/b.py"]
