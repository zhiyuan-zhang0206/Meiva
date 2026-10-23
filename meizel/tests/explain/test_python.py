from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, run


def explain_text(root: Path, path: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert run(root, "explain", path) == 0
    return capsys.readouterr().out


def test_input_append_observations_keep_fresh_rebound_and_nested_mutation_separate(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "sample.py": """
        def direct(values: list[int]):
            values.append(1)
        def fresh(values: list[int]):
            local = list(values)
            local.append(1)
        def rebound(values: list[int]):
            values = list(values)
            values.append(1)
        def nested(values: list[int]):
            def inner(values: list[int]):
                values.append(1)
            thunk = lambda: values.append(2)
            return [values.append(3) for _ in ()]
        def custom(values):
            values.append(1)
        def generic[list](values: list[int]):
            values.append(1)
    """
        }
    )
    analysis = analysis_of(root)
    scan = analysis.scans["sample.py"]
    assert [(item.function, item.parameter) for item in scan.input_appends] == [
        ("direct", "values")
    ]
    # meiva:note
    # Only the trusted scanner value serialized in this assertion is unpickled.
    assert pickle.loads(pickle.dumps(scan)).input_appends == scan.input_appends  # noqa: S301
    assert analysis.diagnostics == analysis.verdict == analysis.facts == []
    text = explain_text(root, "sample.py", capsys)
    assert "direct: values.append(...)" in text
    assert "runtime type/execution unknown" in text
    assert "semantic purity proof" in text


@pytest.mark.parametrize("binding", ["list = tuple", "from external import *"])
def test_shadowed_or_star_import_list_annotation_is_unknown(
    make_repo: RepoFactory,
    binding: str,
) -> None:
    root = make_repo(
        {"sample.py": f"{binding}\ndef bump(values: list[int]):\n    values.append(1)\n"}
    )
    assert analysis_of(root).scans["sample.py"].input_appends == []


def test_explicit_fixture_chain_uses_consuming_test_visibility_and_ignores_unused_definitions(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "conftest.py": """
            import pytest
            @pytest.fixture
            def token():
                return 'parent'
            @pytest.fixture
            def wrapped(token):
                return token
        """,
            "child/conftest.py": """
            import pytest
            @pytest.fixture(name='token')
            def nearer():
                return 'child'
            @pytest.fixture
            def never_requested(operator_database):
                return operator_database
        """,
            "child/test_a.py": "def test_a(wrapped):\n    pass\n",
        }
    )
    analysis = analysis_of(root)
    text = explain_text(root, "child/test_a.py", capsys)
    assert (
        "wrapped [parameter] -> conftest.py:6 wrapped -> token [parameter] -> child/conftest.py:3 nearer"
        in text
    )
    assert "never_requested" not in text and "operator_database" not in text
    assert [
        (r.name, r.line, r.skip_self) for r in analysis.scans["child/test_a.py"].fixtures.requests
    ] == [("wrapped", 1, False)]
    assert {(f.src, f.dst, f.names) for f in analysis.facts if f.kind.value == "fixture"} == {
        ("child/test_a.py", "conftest.py", ("wrapped",)),
    }


def test_same_name_override_chain_uses_registered_identity_and_stops_recursion(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "conftest.py": "import pytest\n@pytest.fixture\ndef token():\n    return 1\n",
            "child/conftest.py": """
            import pytest
            @pytest.fixture(name='token')
            def renamed(token):
                return token
            @pytest.fixture
            def looping(looping):
                return looping
        """,
            "child/test_a.py": "def test_a(token, looping):\n    pass\n",
        }
    )
    text = explain_text(root, "child/test_a.py", capsys)
    assert "child/conftest.py:3 renamed -> token [parameter] -> conftest.py:3 token" in text
    assert "exhausted same-name override chain" in text


def test_markers_parameter_uncertainty_and_conditional_lookup_do_not_invent_dependencies(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "test_a.py": """
        import pytest
        @pytest.fixture
        def declared():
            return 1
        @pytest.mark.usefixtures('not_activated')
        @pytest.fixture
        def marked_fixture():
            return 1
        unused = pytest.mark.usefixtures('never_requested')
        @pytest.mark.usefixtures('declared')
        def test_marked(request, marked_fixture):
            if False:
                request.getfixturevalue('maybe_requested')
        @pytest.mark.parametrize('data', [1], indirect=False)
        def test_param(data):
            pass
        class TestNested:
            def test_nested(self, class_only):
                pass
    """
        }
    )
    text = explain_text(root, "test_a.py", capsys)
    assert "declared [usefixtures] -> test_a.py:3 declared" in text
    assert "never_requested" not in text
    assert (
        "maybe_requested [lookup]: unknown receiver/execution; not a required fixture request"
        in text
    )
    assert "data [parameter]: unknown parametrization" in text
    assert "test_nested: unknown requester ownership (scope or module binding)" in text
    assert "autouse, module marks" in text
    assert (
        "not_activated [usefixtures]: marker on fixture definition; not a required request" in text
    )


def test_explain_cli_preserves_gate_and_prefers_conftest_over_plugin(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "meizel.toml": '[uses]\n"conftest.py" = ["plugin.py"]\n',
            "plugin.py": "import pytest\n@pytest.fixture\ndef resource():\n    return 0\n",
            "conftest.py": "import pytest\npytest_plugins = ['plugin']\n@pytest.fixture\ndef resource():\n    return 1\n",
            "test_a.py": "def test_a(resource):\n    pass\n",
        }
    )
    assert run(root, "check") == 0
    capsys.readouterr()
    assert run(root, "explain", "test_a.py") == 0
    text = capsys.readouterr().out
    assert "resource [parameter] -> conftest.py:4 resource" in text
    assert "resource [parameter] -> plugin.py" not in text
    assert run(root, "check") == 0


def test_ambiguous_plugins_duplicate_registrations_and_imported_alias_stay_unknown(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "one.py": "import pytest\n@pytest.fixture\ndef shared():\n    return 1\n@pytest.fixture\ndef ambiguous():\n    return 1\n",
            "two.py": "import pytest\n@pytest.fixture\ndef ambiguous():\n    return 2\n",
            "conftest.py": "pytest_plugins = ['one', 'two']\n",
            "test_a.py": """
            import pytest
            from one import shared as alias
            @pytest.fixture(name='duplicate')
            def first():
                return 1
            @pytest.fixture(name='duplicate')
            def second():
                return 2
            @pytest.fixture(name=unknown_name)
            def dynamic_fixture():
                return 3
            @pytest.fixture(**unknown_options)
            def dynamic_options():
                return 4
            def test_a(shared, ambiguous, duplicate, alias, dynamic_fixture, dynamic_options):
                pass
        """,
        }
    )
    text = explain_text(root, "test_a.py", capsys)
    assert "shared [parameter] -> one.py:3 shared" in text
    assert "ambiguous [parameter]: unknown plugin registration order" in text
    assert "duplicate [parameter]: unknown definition ownership in test_a.py" in text
    assert "alias [parameter]: unknown definition ownership in test_a.py" in text
    assert "dynamic_fixture [parameter]: unknown definition ownership in test_a.py" in text
    assert "dynamic_options [parameter]: unknown definition ownership in test_a.py" in text


def test_pattern_captures_are_local_rebindings_not_input_list_observations(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "sample.py": """
        def star(values: list[int], replacement: list[int]):
            match replacement:
                case [*values]:
                    values.append(1)
        def capture(values: list[int], replacement: list[int]):
            match replacement:
                case values:
                    values.append(1)
        def mapping(values: list[int], replacement: dict[str, int]):
            match replacement:
                case {**values}:
                    values.append(1)
    """
        }
    )
    assert analysis_of(root).scans["sample.py"].input_appends == []


def test_diamond_fixture_display_bounds_total_work_without_claiming_complete_paths(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    definitions = ["import pytest\n", "@pytest.fixture\ndef leaf():\n    return 1\n"]
    previous = "leaf"
    for level in range(8):
        for side in ("left", "right"):
            definitions.append(f"@pytest.fixture\ndef {side}{level}({previous}):\n    return 1\n")
        previous = f"left{level}, right{level}"
    root = make_repo(
        {
            "conftest.py": "\n".join(definitions),
            "test_a.py": "def test_a(left7):\n    pass\n",
        }
    )
    text = explain_text(root, "test_a.py", capsys)
    assert "display limit reached; additional request paths unknown" in text
    assert len(text) < 40000


def test_rebound_deleted_fixture_and_test_bindings_have_unknown_ownership(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "test_a.py": """
        import pytest
        @pytest.fixture
        def secret():
            return 1
        @pytest.fixture
        def resource(secret):
            return secret
        resource = 42
        @pytest.fixture
        def deleted(secret):
            return secret
        del deleted
        def test_a(resource, deleted):
            pass
        def test_rebound(secret):
            pass
        test_rebound = 42
        class TestScope:
            def test_class(self, secret):
                pass
    """
        }
    )
    text = explain_text(root, "test_a.py", capsys)
    assert "resource [parameter]: unknown definition ownership in test_a.py" in text
    assert "deleted [parameter]: unknown definition ownership in test_a.py" in text
    assert "test_rebound: unknown requester ownership (scope or module binding)" in text
    assert "test_class: unknown requester ownership (scope or module binding)" in text
