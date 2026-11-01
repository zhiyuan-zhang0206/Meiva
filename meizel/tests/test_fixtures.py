from __future__ import annotations

from pathlib import Path

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, edges, verdict


def fixture_edges(root: Path) -> set[tuple[str, str]]:
    return {(s, d) for s, d, k in edges(root) if k == "fixture"}


DEFINE = "import pytest\n\n@pytest.fixture\ndef {name}():\n    return 1\n"


def test_a_test_resolves_a_fixture_to_the_nearest_conftest_above_it(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "pkg/tests/conftest.py": DEFINE.format(name="shared"),
            "pkg/tests/test_a.py": "def test_a(shared):\n    pass\n",
            "other/tests/test_b.py": "def test_b(shared):\n    pass\n",
        }
    )
    assert fixture_edges(root) == {
        ("pkg/tests/test_a.py", "pkg/tests/conftest.py"),
        ("other/tests/test_b.py", "conftest.py"),
    }


def test_ancestor_conftests_need_no_declaration(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "pkg/tests/test_a.py": "def test_a(shared):\n    pass\n",
        }
    )
    assert verdict(root) == set()


def test_a_fixture_defined_in_the_test_module_is_local(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": DEFINE.format(name="shared") + "\ndef test_a(shared):\n    pass\n",
        }
    )
    assert fixture_edges(root) == set()


def test_an_overriding_fixture_that_requests_its_own_name_reaches_the_parent(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "pkg/conftest.py": "import pytest\n\n@pytest.fixture\ndef shared(shared):\n    return shared\n",
        }
    )
    assert fixture_edges(root) == {("pkg/conftest.py", "conftest.py")}


def test_a_conftest_in_another_branch_is_not_visible(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "a/conftest.py": DEFINE.format(name="only_a"),
            "b/tests/test_b.py": "def test_b(only_a):\n    pass\n",
        }
    )
    assert fixture_edges(root) == set()


def test_parametrized_names_defaults_and_helpers_are_not_requests(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": (
                "import pytest\n\n"
                "@pytest.mark.parametrize('shared', [1])\n"
                "def test_a(shared):\n    pass\n\n"
                "def test_b(shared=3):\n    pass\n\n"
                "def helper(shared):\n    pass\n"
            ),
        }
    )
    assert fixture_edges(root) == set()


def test_indirect_parametrization_requests_the_fixture(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": (
                "import pytest\n\n"
                "@pytest.mark.parametrize('shared', [1], indirect=True)\n"
                "def test_a(shared):\n    pass\n"
            ),
        }
    )
    assert fixture_edges(root) == {("t/test_a.py", "conftest.py")}


def test_usefixtures_and_getfixturevalue_request_by_literal_name(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="one")
            + DEFINE.format(name="two").replace("import pytest\n\n", ""),
            "t/test_a.py": (
                "import pytest\n\n"
                "@pytest.mark.usefixtures('one')\n"
                "def test_a(request):\n    request.getfixturevalue('two')\n"
            ),
        }
    )
    found = {f.names for f in analysis_of(root).facts if f.kind.value == "fixture"}
    assert found == {("one",), ("two",)}


def test_mock_arguments_injected_by_patch_decorators_are_not_requests(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="mocked"),
            "lib/__init__.py": "",
            "lib/m.py": "x = 1\n",
            "t/test_a.py": (
                "from unittest import mock\nfrom lib import m\n\n"
                "@mock.patch.object(m, 'x')\n"
                "def test_a(mocked):\n    pass\n"
            ),
        }
    )
    assert fixture_edges(root) == set()


def test_class_methods_skip_self(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": "class TestK:\n    def test_a(self, shared):\n        pass\n",
        }
    )
    assert fixture_edges(root) == {("t/test_a.py", "conftest.py")}


def plugin_repo(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "tests/__init__.py": "",
        "tests/fixtures/__init__.py": "",
        "tests/fixtures/db.py": DEFINE.format(name="db"),
        "conftest.py": "pytest_plugins = ['tests.fixtures.db']\n",
        "base/x/tests/test_a.py": "def test_a(db):\n    pass\n",
        **(extra or {}),
    }


def test_a_literal_pytest_plugins_module_defines_fixtures_for_everything_below(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(plugin_repo())
    assert fixture_edges(root) == {("base/x/tests/test_a.py", "tests/fixtures/db.py")}
    assert ("conftest.py", "tests/fixtures/db.py", "plugin") in edges(root)


def test_plugin_fixtures_are_ordinary_edges_that_must_be_declared_and_exported(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(plugin_repo())
    found = {(d.code.value, d.path) for d in diagnostics(root, "undeclared-edge")}
    assert found == {
        ("undeclared-edge", "conftest.py"),
        ("undeclared-edge", "base/x/tests/test_a.py"),
    }


def test_declared_plugin_edges_pass(make_repo: RepoFactory) -> None:
    root = make_repo(
        plugin_repo(
            {
                "meizel.toml": '[uses]\n"conftest.py" = ["tests"]\n"base" = ["tests"]\n',
                "tests/meizel.toml": 'exports = ["fixtures"]\n',
                "tests/fixtures/meizel.toml": 'exports = ["db.py"]\n',
            }
        )
    )
    assert verdict(root) == set()


def test_requesting_a_private_fixture_name_is_a_private_use(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="_secret"),
            "t/test_a.py": "def test_a(_secret):\n    pass\n",
        }
    )
    [found] = diagnostics(root, "private-name")
    assert found.path == "t/test_a.py"


def test_an_underscore_fixture_works_through_autouse_and_in_its_own_file(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "conftest.py": (
                "import pytest\n\n@pytest.fixture(autouse=True)\ndef _quiet():\n    return 1\n"
            ),
            "t/test_a.py": (
                "import pytest\n\n@pytest.fixture\ndef _local():\n    return 1\n\n"
                "def test_a(_local):\n    pass\n"
            ),
        }
    )
    assert verdict(root) == set()


def test_pure_helper_fixture_in_mixed_conftest_allowed_in_strict_tests(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "meizel.policy.toml": render_policy({}, {}) + "\n[purity]\nimpure-fixtures = []\n",
            "infra/__init__.py": "",
            "infra/db.py": "import socket\nclass DBConn:\n    pass\n",
            "infra/meizel.toml": 'purity = "none"\nexports = ["db.py"]\n',
            "conftest.py": (
                "import pytest\n"
                "from infra.db import DBConn\n\n"
                "@pytest.fixture\n"
                "def db_conn():\n"
                "    return DBConn()\n\n"
                "@pytest.fixture\n"
                "def pure_payload():\n"
                "    return {'x': 1, 'y': 2}\n"
            ),
            "core/__init__.py": "",
            "core/model.py": "def calc(p: dict) -> int:\n    return p['x'] + p['y']\n",
            "core/meizel.toml": 'purity = "strict"\n',
            "core/tests/__init__.py": "",
            "core/tests/test_model.py": (
                "from core.model import calc\n\n"
                "def test_calc(pure_payload):\n"
                "    assert calc(pure_payload) == 3\n"
            ),
            "meizel.toml": '[uses]\n"core" = ["infra"]\n"core/tests" = ["core"]\n',
        }
    )
    assert diagnostics(root, "impure-test-fixture") == []


def test_fixture_taint_graph_catches_transitive_impure_fixtures_without_string_config(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "meizel.policy.toml": render_policy({}, {}) + "\n[purity]\nimpure-fixtures = []\n",
            "infra/__init__.py": "",
            "infra/db.py": "import socket\nclass Transport:\n    pass\n",
            "infra/meizel.toml": 'purity = "none"\nexports = ["db.py"]\n',
            "conftest.py": (
                "import pytest\n"
                "from infra.db import Transport\n\n"
                "@pytest.fixture\n"
                "def raw_transport():\n"
                "    return Transport()\n\n"
                "@pytest.fixture\n"
                "def database_session(raw_transport):\n"
                "    return raw_transport\n\n"
                "@pytest.fixture\n"
                "def api_client(database_session):\n"
                "    return database_session\n"
            ),
            "core/__init__.py": "",
            "core/domain.py": "def logic() -> int:\n    return 42\n",
            "core/meizel.toml": 'purity = "strict"\n',
            "core/tests/__init__.py": "",
            "core/tests/test_domain.py": (
                "from core.domain import logic\n\n"
                "def test_via_client(api_client):\n"
                "    assert logic() == 42\n"
            ),
            "meizel.toml": '[uses]\n"core" = ["infra"]\n"core/tests" = ["core"]\n',
        }
    )
    found = diagnostics(root, "impure-test-fixture")
    assert len(found) == 1
    assert "test of strict modules requests impure fixture `api_client`" in found[0].message
    assert found[0].path == "core/tests/test_domain.py"
