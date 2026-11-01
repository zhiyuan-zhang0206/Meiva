from __future__ import annotations

from pathlib import Path

import pytest

from meizel import allowances, roles
from meizel.graph.model import MeizelError
from meizel.tests.synthetic import STANDALONE_PROJECT, RepoFactory, diagnostics, seat

ROOT = {"a/__init__.py": "", "b/__init__.py": "", "t/test_x.py": "", ".hidden/x.py": ""}
GOOD = '[roles]\napplication = ["a", "b"]\ntests = ["t"]\n'


def problems(make_repo: RepoFactory, manifest: str) -> list[str]:
    root = make_repo({**ROOT, "meizel.toml": manifest})
    return [d.message for d in diagnostics(root, "manifest-roles")]


def test_a_complete_declaration_is_clean(make_repo: RepoFactory) -> None:
    assert problems(make_repo, GOOD) == []


def test_a_top_level_directory_with_python_and_no_role_is_reported(make_repo: RepoFactory) -> None:
    found = problems(make_repo, '[roles]\napplication = ["a"]\ntests = ["t"]\n')
    assert found == [
        "top-level directory 'b' holds Python or TypeScript but has no role; declare it under [roles]"
    ]


def test_a_hidden_directory_is_not_a_package(make_repo: RepoFactory) -> None:
    assert not any(".hidden" in message for message in problems(make_repo, GOOD))


@pytest.mark.parametrize(
    ("manifest", "fragment"),
    [
        ('[roles]\nmystery = ["a"]\n', "unknown role 'mystery'"),
        ('[roles]\napplication = ["a", "b"]\ntests = ["t", "a"]\n', "'a' is in role 'application'"),
        ('[roles]\napplication = ["a", "b", "ghost"]\ntests = ["t"]\n', "'ghost' is not a tracked"),
        ('[roles]\napplication = "a"\n', "must be a list of strings"),
        ("roles = 3\n", "[roles] must be a table"),
    ],
)
def test_a_malformed_declaration_is_reported(
    make_repo: RepoFactory, manifest: str, fragment: str
) -> None:
    assert any(fragment in message for message in problems(make_repo, manifest))


def test_roles_belong_in_the_root_manifest_only(make_repo: RepoFactory) -> None:
    root = make_repo({**ROOT, "a/meizel.toml": GOOD})
    messages = [d.message for d in diagnostics(root, "manifest-roles")]
    assert messages == ["[roles] belongs in the root manifest only"]


def test_the_api_reads_roles_in_declared_order(make_repo: RepoFactory) -> None:
    declared = roles.parse_roles(GOOD, label="meizel.toml")
    assert dict(declared) == {"application": ("a", "b"), "tests": ("t",)}
    assert roles.select_packages(declared, "tests", "application") == ("t", "a", "b")
    assert roles.select_packages(declared, "tooling") == ()
    assert roles.all_packages(declared) == ("a", "b", "t")


def test_the_api_refuses_an_unknown_role(make_repo: RepoFactory) -> None:
    with pytest.raises(MeizelError, match="unknown role"):
        roles.select_packages(roles.parse_roles(GOOD, label="meizel.toml"), "mystery")


def test_the_model_reads_no_file_of_its_own() -> None:
    # meiva:note
    # The model decides; the shell reads. A `GitRepo`, a `Path` or a manifest constant here would put
    # the read back inside the module that is supposed to receive the answer.
    source = Path(roles.__file__).read_text(encoding="utf-8")
    for name in ("GitRepo", "roles_at", "read_text", "Path(", "tomllib.load("):
        assert name not in source, name


@pytest.mark.parametrize("manifest", ["[uses]\n", "[roles]\nmystery = []\n", "not toml ["])
def test_the_api_fails_fast_on_an_invalid_declaration(
    make_repo: RepoFactory, manifest: str
) -> None:
    with pytest.raises(MeizelError):
        roles.parse_roles(manifest, label="meizel.toml")


# meiva:note
# The roles a repository declares, parsed from its root manifest's text: the decision is the
# model's, the read is the test's own.
def declared(root: Path) -> dict[str, tuple[str, ...]]:
    return roles.parse_roles(
        (root / "meizel.toml").read_text(encoding="utf-8"), label="meizel.toml"
    )


def standalone_files() -> dict[str, str]:
    return {
        "meizel.toml": '[roles]\nstandalone = ["runtimes"]\n',
        "runtimes/embedding/pyproject.toml": STANDALONE_PROJECT,
        "runtimes/embedding/uv.lock": "",
        "runtimes/embedding/tests/test_service.py": "",
        "runtimes/embedding/src/meiva_embedding_service/__init__.py": "",
        "runtimes/embedding/src/meiva_embedding_service/config.py": "class Config: pass\n",
        "runtimes/embedding/src/meiva_embedding_service/worker.py": "",
    }


def test_standalone_is_a_distinct_closed_role(make_repo: RepoFactory) -> None:
    root = make_repo(standalone_files())
    assert roles.select_packages(declared(root), "standalone") == ("runtimes",)
    assert roles.select_packages(declared(root), "application") == ()
    assert roles.all_packages(declared(root)) == ("runtimes",)
    assert diagnostics(root, "manifest-roles") == []


def test_native_and_repo_import_names_resolve_to_the_same_files(make_repo: RepoFactory) -> None:
    from meizel.graph.model import Sources
    from meizel.language.scan import scan_source

    root = make_repo(standalone_files())
    universe = seat(root)
    universe = universe.context("runtimes/embedding/src/meiva_embedding_service/worker.py")
    physical = "runtimes/embedding/src/meiva_embedding_service/config.py"
    assert universe.resolve_module("meiva_embedding_service.config") == universe.resolve_module(
        "runtimes.embedding.src.meiva_embedding_service.config"
    )
    assert universe.is_first_party_name("meiva_embedding_service.worker")
    worker = universe.resolve_module("meiva_embedding_service.worker")
    assert worker is not None
    assert worker.file == "runtimes/embedding/src/meiva_embedding_service/worker.py"
    caller = "runtimes/embedding/src/meiva_embedding_service/worker.py"
    for source in (
        "from .config import Config\n",
        "from meiva_embedding_service.config import Config\n",
    ):
        scan = scan_source(universe, caller, source.encode(), Sources(), policy=allowances.Policy())
        assert [fact.dst for fact in scan.facts] == [physical]


def test_native_alias_does_not_bypass_private_module_governance(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            **standalone_files(),
            "runtimes/embedding/src/meiva_embedding_service/_private.py": "value = 1\n",
            "runtimes/embedding/tools/consumer.py": "import meiva_embedding_service._private\n",
            "runtimes/embedding/pyproject.toml": STANDALONE_PROJECT.replace(
                'include = ["src", "tests"]', 'include = ["src", "tests", "tools"]'
            ),
            "runtimes/embedding/meizel.toml": '[uses]\n"tools" = ["src"]\n',
            "runtimes/embedding/src/meizel.toml": 'exports = ["meiva_embedding_service"]\n',
            "runtimes/embedding/src/meiva_embedding_service/meizel.toml": 'exports = ["config.py"]\n',
        }
    )
    assert [site.path for site in diagnostics(root, "private-module")] == [
        "runtimes/embedding/tools/consumer.py"
    ]


@pytest.mark.parametrize(
    ("project", "extra", "fragment"),
    [
        ('[build-system]\nbuild-backend = "setuptools.build_meta"\n', {}, "explicit Hatch"),
        ('[build-system]\nbuild-backend = "hatchling.build"\n', {}, "nonempty Hatch"),
        (
            STANDALONE_PROJECT.replace("src/meiva_embedding_service", "../escape"),
            {},
            "invalid wheel",
        ),
        (
            STANDALONE_PROJECT.replace("src/meiva_embedding_service", "/absolute"),
            {},
            "invalid wheel",
        ),
        (
            STANDALONE_PROJECT.replace("src/meiva_embedding_service", "src/bad-name"),
            {},
            "invalid wheel",
        ),
        (
            STANDALONE_PROJECT.replace("src/meiva_embedding_service", "src/missing"),
            {},
            "no Python source",
        ),
        (
            STANDALONE_PROJECT.replace(
                "[tool.hatch.build.targets.wheel]",
                '[tool.hatch.build]\nsources = ["src"]\n[tool.hatch.build.targets.wheel]',
            ),
            {},
            "source rewrites",
        ),
        (STANDALONE_PROJECT, {"meiva_embedding_service/__init__.py": ""}, "collides"),
        (
            STANDALONE_PROJECT,
            {
                "runtimes/other/pyproject.toml": STANDALONE_PROJECT,
                "runtimes/other/uv.lock": "",
                "runtimes/other/tests/test_service.py": "",
                "runtimes/other/src/meiva_embedding_service/__init__.py": "",
            },
            "collides",
        ),
    ],
)
def test_invalid_or_ambiguous_standalone_layout_fails_fast(
    make_repo: RepoFactory, project: str, extra: dict[str, str], fragment: str
) -> None:

    root = make_repo({**standalone_files(), "runtimes/embedding/pyproject.toml": project, **extra})
    with pytest.raises(MeizelError, match=fragment):
        seat(root)


def test_undeclared_layout_is_not_guessed(make_repo: RepoFactory) -> None:

    root = make_repo({"meizel.toml": '[roles]\nstandalone = ["runtimes"]\n', "runtimes/a.py": ""})
    with pytest.raises(MeizelError, match="no explicit project metadata"):
        seat(root)


@pytest.mark.parametrize("name", ["numpy", "PIL", "ruff"])
def test_native_alias_is_scoped_to_its_project(make_repo: RepoFactory, name: str) -> None:
    from meizel.graph.model import Sources
    from meizel.language.scan import scan_source

    files = {
        path.replace("meiva_embedding_service", name): text.replace("meiva_embedding_service", name)
        for path, text in standalone_files().items()
    }
    files["core/caller.py"] = ""
    universe = seat(make_repo(files))
    source = f'import {name}\nimport subprocess, sys\nsubprocess.run([sys.executable, "-m", "{name}.worker"])\n'
    assert universe.resolve_module(f"{name}.worker") is None
    assert not universe.is_first_party_name(name)
    root_scan = scan_source(
        universe, "core/caller.py", source.encode(), Sources(), policy=allowances.Policy()
    )
    assert root_scan.facts == []
    own_scan = scan_source(
        universe,
        f"runtimes/embedding/src/{name}/config.py",
        source.encode(),
        Sources(),
        policy=allowances.Policy(),
    )
    assert {fact.dst for fact in own_scan.facts} == {
        f"runtimes/embedding/src/{name}/__init__.py",
        f"runtimes/embedding/src/{name}/worker.py",
    }


@pytest.mark.parametrize("name", ["os", "sys", "_io", "asyncio"])
def test_native_alias_cannot_shadow_the_standard_library(make_repo: RepoFactory, name: str) -> None:

    files = {
        path.replace("meiva_embedding_service", name): text.replace("meiva_embedding_service", name)
        for path, text in standalone_files().items()
    }
    with pytest.raises(MeizelError, match="standard library"):
        seat(make_repo(files))


@pytest.mark.parametrize("spelling", [".state", "meiva_embedding_service.state"])
def test_native_and_relative_ambient_imports_have_identical_identity(
    make_repo: RepoFactory, spelling: str
) -> None:
    from meizel.contracts.analysis import analyze
    from meizel.graph.model import Code

    package = "runtimes/embedding/src/meiva_embedding_service"
    caller = package + "/worker.py"
    root = make_repo(
        {
            **standalone_files(),
            package + "/state.py": "settings = object()\n",
            package + "/meizel.toml": '[state]\n"state.py" = ["settings"]\n',
            "meizel.policy.toml": '[ambient]\nsettings = ["settings"]\n',
            caller: f"from {spelling} import settings\nvalue = settings\n",
        }
    )
    result = analyze(seat(root), jobs=1)
    assert [(item.kind.value, item.target) for item in result.scans[caller].ambient] == [
        ("settings", "runtimes.embedding.src.meiva_embedding_service.state.settings")
    ]
    assert [
        (item.path, item.line) for item in result.diagnostics if item.code is Code.AMBIENT_READ
    ] == [(caller, 1)]


def test_standalone_project_listing_is_the_validated_metadata_owner(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    from meizel.tests.synthetic import run

    root = make_repo(standalone_files())
    assert run(root, "roles", "projects") == 0
    assert capsys.readouterr().out == "runtimes/embedding\n"


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("cpu = []", "gpu = []", "CI ownership"),
        ('typeCheckingMode = "strict"', 'typeCheckingMode = "basic"', "CI ownership"),
        ('include = ["src", "tests"]', 'include = ["tests"]', "does not include"),
        ('testpaths = ["tests"]', 'testpaths = ["../tests"]', "relative directory"),
        ('testpaths = ["tests"]', 'testpaths = ["missing"]', "does not include"),
    ],
)
def test_standalone_project_cannot_silently_drop_its_check_ownership(
    make_repo: RepoFactory, old: str, new: str, fragment: str
) -> None:

    files = standalone_files()
    files["runtimes/embedding/pyproject.toml"] = STANDALONE_PROJECT.replace(old, new)
    with pytest.raises(MeizelError, match=fragment):
        seat(make_repo(files))


def test_standalone_python_outside_a_project_is_rejected(make_repo: RepoFactory) -> None:

    with pytest.raises(MeizelError, match="no owning project"):
        seat(make_repo({**standalone_files(), "runtimes/orphan.py": ""}))


def test_project_python_outside_strict_includes_is_rejected(make_repo: RepoFactory) -> None:

    with pytest.raises(MeizelError, match=r"does not include source 'helper\.py'"):
        seat(make_repo({**standalone_files(), "runtimes/embedding/helper.py": ""}))


def test_project_listing_refuses_an_unknown_role(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    from meizel.tests.synthetic import run

    root = make_repo({"meizel.toml": "[roles]\nmystery = []\n"})
    assert run(root, "roles", "projects") == 2
    assert "unknown role" in capsys.readouterr().err
