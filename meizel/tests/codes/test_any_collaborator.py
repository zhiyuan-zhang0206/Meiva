from __future__ import annotations

from pathlib import Path

import pytest

from meizel import allowances
from meizel.contracts.analysis import analyze, diagnose, load_policy, scan_all
from meizel.contracts.cli import GitRepo
from meizel.graph.model import AnyCollaboratorSite, Code, Sources
from meizel.graph.universe import Universe
from meizel.language.scan import FileScan, scan_source
from meizel.tests.synthetic import (
    RepoFactory,
    analysis_of,
    commit,
    counts,
    git,
    run,
    seat,
    write,
)


def scan(tmp_path: Path, source: str, path: str = "app.py") -> FileScan:
    return scan_source(
        Universe(GitRepo(tmp_path), [path]),
        path,
        source.encode("utf-8"),
        Sources(),
        policy=allowances.Policy(),
    )


def test_canonical_aliases_match_parameter_return_and_class_field(tmp_path: Path) -> None:
    source = (
        "import typing\n"
        "import typing as t\n"
        "from typing import Any as Dynamic\n"
        "class Box:\n"
        "    field: Dynamic\n"
        "    other: typing.Any = 1\n"
        "    def method(self: Dynamic, pos: t.Any, /, *args: Dynamic, kw: typing.Any, **kwargs: t.Any) -> Dynamic:\n"
        "        return pos\n"
        "    def __getattr__(self, name: str) -> Dynamic:\n"
        "        return name\n"
    )
    sites = scan(tmp_path, source).any_collaborators
    assert [(s.qualname, s.slot, s.role, s.line) for s in sites] == [
        ("Box", "field", "class-field", 5),
        ("Box", "other", "class-field", 6),
        ("Box.method", "self", "parameter", 7),
        ("Box.method", "pos", "parameter", 7),
        ("Box.method", "args", "parameter", 7),
        ("Box.method", "kw", "parameter", 7),
        ("Box.method", "kwargs", "parameter", 7),
        ("Box.method", "return", "return", 7),
        ("Box.__getattr__", "return", "return", 9),
    ]


def test_union_and_optional_any_annotations_match(tmp_path: Path) -> None:
    source = (
        "from typing import Any, Optional, Union\n"
        "class C:\n"
        "    c: Any | None\n"
        "    d: Optional[Any]\n"
        "def f(x: None | Any, y: Union[Any, None]) -> Any | None:\n"
        "    return x\n"
    )
    sites = scan(tmp_path, source).any_collaborators
    assert [(s.qualname, s.slot, s.role, s.line) for s in sites] == [
        ("C", "c", "class-field", 3),
        ("C", "d", "class-field", 4),
        ("f", "x", "parameter", 5),
        ("f", "y", "parameter", 5),
        ("f", "return", "return", 5),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from vendor import Any\ndef f(x: Any) -> Any:\n    return x\n",
        "from typing import Any\nAny = int\ndef f(x: Any) -> Any:\n    return x\n",
        "from typing import Any\ndef f[Any](x: Any) -> Any:\n    return x\n",
        "from typing import Any\nclass C[Any]:\n    x: Any\n",
        "from typing import Any\nclass C:\n    Any = int\n    x: Any\n",
        "import builtins\ndef f(x: object, y: builtins.object) -> object:\n    return x\n",
        (
            "from typing import Annotated, Any, Callable\n"
            "class C:\n"
            "    a: list[Any]\n"
            "    b: dict[str, Any]\n"
            "    e: Annotated[Any, 'meta']\n"
            "def f(cb: Callable[..., Any], items: tuple[Any, ...]) -> list[Any]:\n"
            "    return list(items)\n"
        ),
    ],
)
def test_shadowed_names_object_and_container_leaves_do_not_match(
    tmp_path: Path, source: str
) -> None:
    assert scan(tmp_path, source).any_collaborators == []


def test_single_layer_string_annotations_use_lexical_scope_and_constant_coordinates(
    tmp_path: Path,
) -> None:
    source = (
        "import typing as t\n"
        "from typing import Any as Dynamic\n"
        "class C:\n"
        '    field: "Dynamic"\n'
        'def f(x: "t.Any", y: "Dynamic | None") -> "Dynamic":\n'
        "    return x\n"
    )
    assert scan(tmp_path, source).any_collaborators == [
        AnyCollaboratorSite(line=4, column=11, qualname="C", slot="field", role="class-field"),
        AnyCollaboratorSite(line=5, column=9, qualname="f", slot="x", role="parameter"),
        AnyCollaboratorSite(line=5, column=21, qualname="f", slot="y", role="parameter"),
        AnyCollaboratorSite(line=5, column=42, qualname="f", slot="return", role="return"),
    ]


@pytest.mark.parametrize(
    "source",
    [
        'from vendor import Any\ndef f(x: "Any") -> "Any":\n    return x\n',
        'from typing import Any\ndef f[Any](x: "Any") -> "Any":\n    return x\n',
        'from typing import Any\ndef f(x: "list[Any]") -> "object":\n    return x\n',
        'from typing import Any\ndef f(x: "Any[", y: "\'Any\'", z: "\'Any\' | None") -> int:\n    return 1\n',
    ],
)
def test_non_matching_or_malformed_string_annotations_do_not_match(
    tmp_path: Path, source: str
) -> None:
    assert scan(tmp_path, source).any_collaborators == []


def test_non_class_field_annotations_do_not_match(tmp_path: Path) -> None:
    source = (
        "from typing import Any\n"
        "module_var: Any = 1\n"
        "class C:\n"
        "    items[0]: Any = 1\n"
        "    other.attr: Any = 1\n"
        "    self_attr: int\n"
        "    def __init__(self) -> None:\n"
        "        local_var: Any = 1\n"
        "        self.self_attr: Any = local_var\n"
    )
    assert scan(tmp_path, source).any_collaborators == []


def test_same_line_duplicate_class_fields_keep_distinct_diagnostics_and_accumulate_debt(
    make_repo: RepoFactory,
) -> None:
    source = "from typing import Any\nclass C: x: Any; x: Any\n"
    root = make_repo({"app.py": source})
    found = analysis_of(root)
    full = [d for d in found.diagnostics if d.code is Code.ANY_COLLABORATOR]
    single = [
        d
        for d in diagnose(
            found.scans["app.py"], found.manifests, policy=load_policy(found.universe)[0]
        )
        if d.code is Code.ANY_COLLABORATOR
    ]
    assert full == single
    assert len(full) == 2
    assert len({d.message for d in full}) == 2
    assert {d.debt.key for d in full if d.debt is not None} == {"app.py#any C.x"}
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    assert counts(analysis_of(root).manifests[""].debt[Code.ANY_COLLABORATOR]) == {
        "app.py#any C.x": 2
    }
    assert not analysis_of(root).verdict
    write(
        root,
        {"app.py": "from typing import Any\nclass C:\n    x: Any\n    x: Any\n    x: Any\n"},
    )
    assert [d.code for d in analysis_of(root).verdict] == [Code.ANY_COLLABORATOR] * 3


def test_debt_key_ignores_line_drift_and_tracks_file_moves(make_repo: RepoFactory) -> None:
    source = "from typing import Any\ndef f(x: Any, y: Any) -> int:\n    return 1\n"
    root = make_repo({"app.py": source})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    assert counts(analysis_of(root).manifests[""].debt[Code.ANY_COLLABORATOR]) == {
        "app.py#any f.x": 1,
        "app.py#any f.y": 1,
    }
    write(root, {"app.py": "# header comment\n\n" + source})
    assert not analysis_of(root).verdict

    write(root, {"app.py": source.replace(", y: Any", "")})
    assert run(root, "debt", "prune") == 0
    assert counts(analysis_of(root).manifests[""].debt[Code.ANY_COLLABORATOR]) == {
        "app.py#any f.x": 1
    }

    commit(root)
    (root / "app.py").rename(root / "moved.py")
    git(root, "add", "-A")
    assert run(root, "debt", "carry") == 0
    assert counts(analysis_of(root).manifests[""].debt[Code.ANY_COLLABORATOR]) == {
        "moved.py#any f.x": 1
    }


def test_unknown_any_collaborator_policy_table_is_rejected(make_repo: RepoFactory) -> None:
    root = make_repo({"app.py": "from typing import Any\ndef f(x: Any) -> int:\n    return 1\n"})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    policy = (root / "meizel.policy.toml").read_text(encoding="utf-8")
    write(
        root,
        {
            "meizel.policy.toml": (
                policy + '\n[any-collaborator.files]\n"app.py" = "not an allowance"\n'
            )
        },
    )
    assert any(d.code is Code.POLICY_SYNTAX for d in analysis_of(root).verdict)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_app.py",
        "pkg/tests/helper.py",
        "pkg/conftest.py",
        "pkg/test_unit.py",
        "pkg/unit_test.py",
    ],
)
def test_test_and_helper_paths_are_excluded(tmp_path: Path, path: str) -> None:
    source = "from typing import Any\ndef f(x: Any) -> Any:\n    return x\n"
    assert scan(tmp_path, source, path).any_collaborators == []


def test_entry_consistency_and_process_pool_across_200_files(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            f"app_{index}.py": f"from typing import Any\ndef f_{index}(x: Any) -> int:\n    return {index}\n"
            for index in range(200)
        }
    )
    found = analyze(seat(root), jobs=2)
    assert len(found.scans) == 200
    assert all(len(scanned.any_collaborators) == 1 for scanned in found.scans.values())
    assert len([d for d in found.diagnostics if d.code is Code.ANY_COLLABORATOR]) == 200

    single = diagnose(
        found.scans["app_0.py"], found.manifests, policy=load_policy(found.universe)[0]
    )
    assert [d for d in single if d.code is Code.ANY_COLLABORATOR] == [
        d for d in found.diagnostics if d.path == "app_0.py" and d.code is Code.ANY_COLLABORATOR
    ]
    pooled = scan_all(found.universe, Sources(), jobs=2, policy=load_policy(found.universe)[0])
    assert pooled["app_0.py"].any_collaborators == found.scans["app_0.py"].any_collaborators


def test_strict_owner_and_unauthorized_key_fail_check(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "strict_pkg/meizel.toml": "",
            "strict_pkg/app.py": "from typing import Any\ndef f(x: Any) -> int:\n    return 1\n",
        }
    )
    verdict = analysis_of(root).verdict
    assert [d.code for d in verdict] == [Code.ANY_COLLABORATOR]
    hint_text = "\n".join(verdict[0].hints)
    assert "Protocol" in hint_text
    assert "TypeGuard/TypeIs" in hint_text
    assert "do not use typing.cast, which [type-cast] forbids" in hint_text
