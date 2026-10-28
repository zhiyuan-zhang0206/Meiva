from __future__ import annotations

from pathlib import Path

import pytest

from meizel.contracts.analysis import analyze, diagnose, load_policy
from meizel.graph.model import Code, Sources
from meizel.graph.universe import GitRepo, Universe
from meizel.language.scan import FileScan, scan_source
from meizel.tests.synthetic import RepoFactory, analysis_of, commit, git, run, seat, write


def scan(tmp_path: Path, source: str, path: str = "app.py") -> FileScan:
    return scan_source(Universe(GitRepo(tmp_path), [path]), path, source.encode(), Sources())


@pytest.mark.parametrize(
    "source",
    [
        "from typing import cast\ncast(int, value)",
        "from typing import cast as narrow\nnarrow('int', value)",
        "import typing as t\nt.cast(int, value)",
        "from typing import cast\nif TYPE_CHECKING:\n    cast(int, value)",
    ],
)
def test_canonical_calls_are_recorded(tmp_path: Path, source: str) -> None:
    [site] = scan(tmp_path, source).type_casts
    assert site.qualname == "<module>"


@pytest.mark.parametrize(
    "source",
    [
        "def cast(t, value): pass\ncast(int, value)",
        "from typing import cast\ndef f(cast): return cast(int, value)",
        "from typing import cast\ncast = provider\ncast(int, value)",
        "from typing import cast\nfor cast in providers: cast(int, value)",
        "from typing import cast\ntry: pass\nexcept Exception as cast: cast(int, value)",
        "from typing import cast\nmatch provider:\n    case cast: cast(int, value)",
        "from typing import cast\nf = lambda cast: cast(int, value)",
        "from typing import cast\nvalues = [cast(int, value) for cast in providers]",
        "from ctypes import cast\ncast(value, target)",
        "import other\nother.cast(int, value)",
        "from typing import cast\ncast\ntext = 'cast(int, value)'",
    ],
)
def test_noncanonical_names_and_references_are_not_calls(tmp_path: Path, source: str) -> None:
    assert scan(tmp_path, source).type_casts == []


@pytest.mark.parametrize(
    "header",
    [
        "def f(x=(t := provider)):",
        "def f(*, x=(t := provider)):",
        "async def f(x=(t := provider)):",
        "@(t := provider)\ndef f():",
        "def f(x: (t := provider)):",
        "def f() -> (t := provider):",
        "class C(t := provider):",
        "class C(metaclass=(t := provider)):",
        "@(t := provider)\nclass C:",
    ],
)
def test_header_rebinding_does_not_report_a_non_typing_call(tmp_path: Path, header: str) -> None:
    source = "import typing as t\n" + header + "\n    pass\nt.cast(int, value)\n"
    assert scan(tmp_path, source).type_casts == []


@pytest.mark.parametrize("definition", ["def f():", "async def f():", "class C:"])
def test_definition_body_bindings_do_not_leak_outward(tmp_path: Path, definition: str) -> None:
    source = "import typing as t\n" + definition + "\n    t = provider\nt.cast(int, value)\n"
    assert [site.qualname for site in scan(tmp_path, source).type_casts] == ["<module>"]


def test_named_body_and_header_qualnames_follow_evaluation_scopes(tmp_path: Path) -> None:
    source = """from typing import cast
@cast(object, decorator)
class C(cast(type, Base), metaclass=cast(type, Meta)):
    @cast(object, decorator)
    def method(self, x=cast(int, value)) -> cast(type, Result):
        def inner(x=cast(int, value)):
            return cast(int, value)
        return cast(int, value)
"""
    assert [site.qualname for site in scan(tmp_path, source).type_casts] == [
        "<module>",
        "<module>",
        "<module>",
        "C",
        "C",
        "C",
        "C.method",
        "C.method.inner",
        "C.method",
    ]


@pytest.mark.parametrize(
    ("expression", "qualnames"),
    [
        ("lambda x=cast(int, v): cast(int, x)", ["f", "f.<lambda>"]),
        ("[cast(int, x) for x in cast(list, values)]", ["f", "f.<listcomp>"]),
        ("{cast(int, x) for x in values}", ["f.<setcomp>"]),
        ("{x: cast(int, x) for x in values}", ["f.<dictcomp>"]),
        (
            "(cast(int, y) for x in values for y in cast(list, x) if cast(bool, y))",
            ["f.<genexpr>"] * 3,
        ),
        ("(lambda: cast(int, v), lambda: cast(int, v))", ["f.<lambda>"] * 2),
    ],
)
def test_anonymous_groups_keep_stable_tokens_and_outer_headers(
    tmp_path: Path, expression: str, qualnames: list[str]
) -> None:
    source = "from typing import cast\ndef f():\n    return " + expression + "\n"
    assert [site.qualname for site in scan(tmp_path, source).type_casts] == qualnames


def test_columns_are_utf8_offsets_and_preserve_same_line_calls(tmp_path: Path) -> None:
    prefix = "label = 'é'; "
    source = "from typing import cast\n" + prefix + "cast(int, a); cast(int, b)\n"
    sites = scan(tmp_path, source).type_casts
    assert [site.line for site in sites] == [2, 2]
    assert [site.column for site in sites] == [
        len(prefix.encode()),
        len((prefix + "cast(int, a); ").encode()),
    ]
    assert len({site.qualname for site in sites}) == 1


@pytest.mark.parametrize(
    "path",
    [
        "tests/helpers.py",
        "app/test/helpers.py",
        "app/test_cast.py",
        "app/cast_test.py",
        "app/conftest.py",
    ],
)
def test_existing_test_path_contract_excludes_helpers(tmp_path: Path, path: str) -> None:
    assert scan(tmp_path, "from typing import cast\ncast(int, value)", path).type_casts == []


@pytest.mark.parametrize("mode", ["none", "strict-to-be", "strict"])
def test_type_cast_is_independent_of_purity_and_partial_scan(
    make_repo: RepoFactory, mode: str
) -> None:
    root = make_repo(
        {
            "app.py": "from typing import cast\ncast(int, value)\n",
            "other.py": "from typing import cast\ncast(int, value)\n",
            "meizel.toml": f'[purity]\n"." = "{mode}"\n',
        }
    )
    found = analyze(seat(root), jobs=1, changed=["app.py"])
    assert [d.path for d in found.diagnostics if d.code is Code.TYPE_CAST] == ["app.py"]
    assert found.scans["app.py"].type_casts


def test_same_line_calls_survive_both_diagnostic_consumers_and_ledger(
    make_repo: RepoFactory,
) -> None:
    source = "from typing import cast\ndef f(): return cast(int, a), cast(int, b)\n"
    root = make_repo({"app.py": source})
    found = analysis_of(root)
    full = [d for d in found.diagnostics if d.code is Code.TYPE_CAST]
    single = [
        d
        for d in diagnose(
            found.scans["app.py"], found.manifests, policy=load_policy(found.universe)[0]
        )
        if d.code is Code.TYPE_CAST
    ]
    assert full == single
    assert len(full) == 2
    assert len({d.message for d in full}) == 2
    assert {d.debt.key for d in full if d.debt is not None} == {"app.py#cast f"}
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    assert analysis_of(root).manifests[""].debt[Code.TYPE_CAST] == {"app.py#cast f": 2}
    assert not analysis_of(root).verdict
    write(root, {"app.py": source.replace("cast(int, b)", "cast(int, b), cast(int, c)")})
    assert [d.code for d in analysis_of(root).verdict] == [Code.TYPE_CAST] * 3


def test_line_movement_preserves_the_key_and_prune_pays_removed_calls(
    make_repo: RepoFactory,
) -> None:
    source = "from typing import cast\ndef f(): return cast(int, a), cast(int, b)\n"
    root = make_repo({"app.py": source})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    write(root, {"app.py": "\n\n" + source})
    assert not analysis_of(root).verdict
    write(root, {"app.py": source.replace(", cast(int, b)", "")})
    assert run(root, "debt", "prune") == 0
    assert analysis_of(root).manifests[""].debt[Code.TYPE_CAST] == {"app.py#cast f": 1}


def test_file_move_carries_type_cast_debt(make_repo: RepoFactory) -> None:
    root = make_repo({"app.py": "from typing import cast\ndef f(): return cast(int, value)\n"})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    commit(root)
    (root / "app.py").rename(root / "moved.py")
    git(root, "add", "-A")
    assert run(root, "debt", "carry") == 0
    assert analysis_of(root).manifests[""].debt[Code.TYPE_CAST] == {"moved.py#cast f": 1}


def test_unknown_type_cast_policy_table_is_rejected_with_a_recorded_site(
    make_repo: RepoFactory,
) -> None:
    root = make_repo({"app.py": "from typing import cast\ncast(int, value)\n"})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    policy = (root / "meizel.policy.toml").read_text()
    write(
        root,
        {"meizel.policy.toml": policy + '\n[type-cast.files]\n"app.py" = "not an allowance"\n'},
    )
    assert any(d.code is Code.POLICY_SYNTAX for d in analysis_of(root).verdict)


def test_process_pool_roundtrip_preserves_source_sites(make_repo: RepoFactory) -> None:
    root = make_repo(
        {f"app_{i}.py": "from typing import cast\ncast(int, value)\n" for i in range(200)}
    )
    found = analyze(seat(root), jobs=2)
    assert len(found.scans) == 200
    assert all(len(scanned.type_casts) == 1 for scanned in found.scans.values())
    assert len([d for d in found.diagnostics if d.code is Code.TYPE_CAST]) == 200


@pytest.mark.parametrize(
    "body",
    [
        "def f[t](x: t.cast(int, value)): pass",
        "def f[t]() -> t.cast(int, value): pass",
        "def f[t](): return t.cast(int, value)",
        "def f[*t](): return t.cast(int, value)",
        "def f[**t](): return t.cast(int, value)",
        "class C[t](t.cast(type, Base)): pass",
        "class C[t]:\n    value = t.cast(int, value)",
        "class C[t]:\n    def method(self): return t.cast(int, value)",
        "type Alias[t] = t.cast(int, value)",
    ],
)
def test_pep695_type_parameters_are_not_import_origins(tmp_path: Path, body: str) -> None:
    assert scan(tmp_path, "import typing as t\n" + body + "\n").type_casts == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("def f[t](x=t.cast(int, value)): pass", ["<module>"]),
        ("@t.cast(object, decorator)\ndef f[t](): pass", ["<module>"]),
        ("@t.cast(object, decorator)\nclass C[t]: pass", ["<module>"]),
        ("class C[t](metaclass=t.cast(type, Meta)): pass", []),
        ("def f[t](): pass\nt.cast(int, value)", ["<module>"]),
        (
            "class C[t]:\n    import typing as t\n    value = t.cast(int, value)\n    def method(self): return t.cast(int, value)",
            ["C"],
        ),
        (
            "class C[t]:\n    import typing as t\n    class Nested:\n        def method(self): return t.cast(int, value)",
            [],
        ),
        ("type Alias[T] = t.cast(int, value)", ["<module>"]),
        ("type Alias = t.cast(int, value)", ["<module>"]),
        ("type t = int\nt.cast(int, value)", []),
        ("type t = int\nsentinel = (unused := 1)\nt.cast(int, value)", []),
        ("type t = t.cast(int, value)", []),
        ("type Alias[T: t.cast(type, value)] = int", ["<module>"]),
        ("type Alias[t: t.cast(type, value)] = int", []),
        ("def f[T: t.cast(type, value)](): pass", []),
        ("class C[T: t.cast(type, value)]: pass", []),
    ],
)
def test_pep695_outer_views_aliases_and_existing_bound_coverage(
    tmp_path: Path, body: str, expected: list[str]
) -> None:
    assert [
        s.qualname for s in scan(tmp_path, "import typing as t\n" + body + "\n").type_casts
    ] == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "def method[T](self, x: local_t.cast(int, value)): return local_t.cast(int, value)",
            ["C"],
        ),
        ("def method[local_t](self, x: local_t.cast(int, value)): pass", []),
        ("def method[local_t](self, x: local_t.cast(int, value)): return local_t", []),
        ("type Alias[T] = local_t.cast(int, value)", ["C"]),
        ("type Alias[local_t] = local_t.cast(int, value)", []),
        ("type Alias[T: local_t.cast(type, value)] = int", ["C"]),
    ],
)
def test_class_annotation_visibility_does_not_leak_into_body_closures(
    tmp_path: Path, body: str, expected: list[str]
) -> None:
    source = "class C:\n    import typing as local_t\n    " + body + "\n"
    assert [s.qualname for s in scan(tmp_path, source).type_casts] == expected


@pytest.mark.parametrize(
    "expression", ["(lambda: t.cast(int, value))", "[t.cast(int, value) for value in values]"]
)
@pytest.mark.parametrize("class_scope", [False, True])
def test_annotation_expression_closures_use_type_parameters_not_class_locals(
    tmp_path: Path, expression: str, class_scope: bool
) -> None:
    if class_scope:
        source = (
            "import typing as t\nclass C[t]:\n    import typing as t\n    def f(self, x: "
            + expression
            + "): pass\n"
        )
    else:
        source = "import typing as t\ndef f[t](x: " + expression + "): pass\n"
    assert scan(tmp_path, source).type_casts == []


@pytest.mark.parametrize(
    ("imported", "expression", "consumer"),
    [
        ("import os", "os.getenv('KEY')", "ambient"),
        ("import time", "time.time()", "purity"),
        ("import time", "setattr(time, 'sleep', replacement)", "patch"),
        ("", "compile('x', '<source>', 'eval')", "restricted"),
    ],
)
def test_alias_bound_keeps_existing_source_consumers(
    tmp_path: Path, imported: str, expression: str, consumer: str
) -> None:
    result = scan(tmp_path, imported + "\ntype Alias[T: " + expression + "] = int\n")
    observed = {
        "ambient": bool(result.ambient),
        "purity": bool(result.purity),
        "patch": bool(result.patches),
        "restricted": bool(result.restricted),
    }
    assert observed[consumer]


def test_generic_fixture_decorator_keeps_outer_metadata_identity(tmp_path: Path) -> None:
    source = "import pytest\n@pytest.fixture\ndef resource[pytest](): return None\n"
    result = scan(tmp_path, source, "conftest.py")
    assert result.fixtures.defs == {"resource"}
    assert result.fixtures.functions[0].module_owned


def test_scope_views_preserve_top_level_parameter_observations(tmp_path: Path) -> None:
    result = scan(tmp_path, "def collect(values: list[int]): values.append(1)\n")
    assert [(item.function, item.parameter) for item in result.input_appends] == [
        ("collect", "values")
    ]


@pytest.mark.parametrize(
    "expression", ["(lambda: t.cast(int, value))", "[t.cast(int, value) for value in values]"]
)
def test_annotation_expression_closures_preserve_real_global_imports(
    tmp_path: Path, expression: str
) -> None:
    source = (
        "import typing as t\nclass C:\n    t = provider\n    def f(self, x: "
        + expression
        + "): pass\n"
    )
    assert len(scan(tmp_path, source).type_casts) == 1


def test_strict_purity_keeps_authorized_cast_counts_but_rejects_growth(
    make_repo: RepoFactory,
) -> None:
    source = "from typing import cast\ncast(int, value)\n"
    root = make_repo({"app.py": source, "meizel.toml": '[purity]\n"." = "strict"\n'})
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    found = analysis_of(root)
    assert found.manifests[""].purity_default == "strict"
    assert found.manifests[""].debt[Code.TYPE_CAST] == {"app.py#cast <module>": 1}
    assert not found.verdict
    write(root, {"app.py": source + "cast(int, other)\n"})
    assert [d.code for d in analysis_of(root).verdict] == [Code.TYPE_CAST] * 2
