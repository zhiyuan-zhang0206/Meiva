from __future__ import annotations

from pathlib import Path

import pytest

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics

ROOT = '[roles]\napplication = ["app", "lib"]\ntests = ["tests"]\n'
READERS = {
    "environ": ["lib.env.process_environment()"],
    "home": ["lib.paths.meiva_home()", "lib.paths.Home.current()"],
}
FILES = {
    "lib/__init__.py": "",
    "lib/meizel.toml": '[roots]\n"env.py" = ["environ"]\n"paths.py" = ["environ", "home"]\n',
    "lib/env.py": "import os\n\n\ndef process_environment():\n    return os.environ\n",
    "lib/paths.py": (
        "import os\nfrom pathlib import Path\n\n\n"
        "def meiva_home():\n    return Path(os.environ['H'])\n\n\n"
        "class Home:\n    @classmethod\n    def current(cls):\n        return Path.home()\n"
    ),
    "tests/__init__.py": "",
    "app/__init__.py": "",
}


def policy(readers: dict[str, list[str]]) -> str:
    sections = [
        f"[ambient-readers.{kind}]\n" + "".join(f'"{name}" = "reads the world"\n' for name in names)
        for kind, names in readers.items()
    ]
    return render_policy({}, {}) + "\n" + "\n".join(sections)


def repo(
    make_repo: RepoFactory,
    files: dict[str, str],
    readers: dict[str, list[str]] | None = None,
    manifest: str = "",
) -> Path:
    return make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": policy(READERS if readers is None else readers),
            **FILES,
            "app/meizel.toml": manifest,
            **files,
        }
    )


# meiva:note
# The (kind, target) pairs a file acquires, whatever the rule judges.
def acquired(make_repo: RepoFactory, source: str) -> list[tuple[str, str]]:
    root = repo(make_repo, {"app/mod.py": source})
    return [(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from lib.env import process_environment\n\nx = process_environment()\n",
            [("environ", "lib.env.process_environment")],
        ),
        (
            "from lib import env\n\nx = env.process_environment()\n",
            [("environ", "lib.env.process_environment")],
        ),
        (
            "import lib.env as e\n\nx = e.process_environment()\n",
            [("environ", "lib.env.process_environment")],
        ),
        (
            "from lib.env import process_environment as environment\n\nx = environment()\n",
            [("environ", "lib.env.process_environment")],
        ),
        (
            "from lib.paths import Home\n\nx = Home.current()\n",
            [("home", "lib.paths.Home.current")],
        ),
        (
            "from lib.paths import meiva_home\n\n\ndef f():\n    return meiva_home()\n",
            [("home", "lib.paths.meiva_home")],
        ),
    ],
)
def test_a_call_to_a_listed_reader_acquires_the_kind_it_is_listed_under(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "from lib.env import process_environment\n\nreader = process_environment\n",
        "from lib import env\n\nreader = env.process_environment\n",
        "def process_environment():\n    return {}\n\n\nx = process_environment()\n",
        "from lib.paths import Home\n\nx = Home\n",
        "from lib.env import process_environment\n\ndef f(process_environment):\n    return process_environment()\n",
        "x = 'lib.env.process_environment()'\n",
    ],
)
def test_naming_a_reader_without_calling_it_or_a_look_alike_acquires_nothing(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []


def test_a_function_of_the_same_name_in_another_module_is_not_the_reader(
    make_repo: RepoFactory,
) -> None:
    files = {
        "app/other.py": "def process_environment():\n    return {}\n",
        "app/mod.py": "from app.other import process_environment\n\nx = process_environment()\n",
        "app/meizel.toml": '[uses]\n"mod.py" = ["other.py"]\n',
    }
    root = repo(make_repo, files)
    assert analysis_of(root).scans["app/mod.py"].ambient == []


def test_a_re_export_is_the_same_reader(make_repo: RepoFactory) -> None:
    files = {
        "lib/__init__.py": "from lib.env import process_environment\n",
        "lib/meizel.toml": '[roots]\n"env.py" = ["environ"]\n',
    }
    root = repo(make_repo, {**files, "app/mod.py": "import lib\n\nx = lib.process_environment()\n"})
    scan = analysis_of(root).scans["app/mod.py"]
    assert [(a.kind.value, a.target) for a in scan.ambient] == [
        ("environ", "lib.process_environment")
    ]


def test_a_rename_in_a_re_export_is_followed(
    make_repo: RepoFactory,
) -> None:
    files = {
        "lib/__init__.py": "from lib.env import process_environment as live\n",
        "lib/meizel.toml": '[roots]\n"env.py" = ["environ"]\n',
        "app/mod.py": "from lib import live\n\nx = live()\n",
    }
    root = repo(make_repo, files)
    scan = analysis_of(root).scans["app/mod.py"]
    assert [(a.kind.value, a.target) for a in scan.ambient] == [("environ", "lib.live")]


def sites(make_repo: RepoFactory, source: str, manifest: str = "") -> list[tuple[str, str]]:
    root = repo(make_repo, {"app/mod.py": source}, manifest=manifest)
    return [
        (d.debt.key, d.message)
        for d in diagnostics(root, "ambient-read")
        if d.path == "app/mod.py" and d.debt is not None
    ]


def test_a_file_outside_a_root_reads_the_world_through_a_reader(make_repo: RepoFactory) -> None:
    found = sites(
        make_repo, "from lib.env import process_environment\n\nx = process_environment()\n"
    )
    assert found == [
        (
            "mod.py#environ lib.env.process_environment",
            "reads the environment (`lib.env.process_environment`) outside a composition root",
        )
    ]


@pytest.mark.parametrize(
    ("kinds", "recorded"),
    [("environ", []), ("home", ["mod.py#environ lib.env.process_environment"])],
)
def test_a_root_of_the_kind_may_call_the_reader_and_a_root_of_another_kind_may_not(
    make_repo: RepoFactory, kinds: str, recorded: list[str]
) -> None:
    source = "from lib.env import process_environment\n\nx = process_environment()\n"
    found = sites(make_repo, source, f'[roots]\n"mod.py" = ["{kinds}"]\n')
    assert [key for key, _ in found] == recorded


def test_the_reader_s_own_module_is_judged_by_what_it_reads(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {})
    assert not [d for d in diagnostics(root, "ambient-read") if d.path.startswith("lib/")]


def test_a_reader_that_no_longer_exists_is_stale(make_repo: RepoFactory) -> None:
    readers = {
        "environ": [
            "lib.env.process_environment()",
            "lib.env.vanished()",
            "lib.gone.read()",
            "lib.paths.Home.vanished()",
        ]
    }
    root = repo(make_repo, {}, readers)
    stale = diagnostics(root, "stale-reader")
    assert [(d.path, d.message.split("] ", 1)[1]) for d in stale] == [
        (
            "meizel.policy.toml",
            "'lib.env.vanished()' names no function or method `vanished` in lib/env.py",
        ),
        (
            "meizel.policy.toml",
            "'lib.gone.read()' names no function or method `gone.read` in lib/__init__.py",
        ),
        (
            "meizel.policy.toml",
            "'lib.paths.Home.vanished()' names no function or method `Home.vanished` in lib/paths.py",
        ),
    ]
    assert [d.line > 1 for d in stale] == [True, True, True]


@pytest.mark.parametrize(
    ("readers", "fragment"),
    [
        ({"environ": ["lib.env.process_environment"]}, "must be a qualified call"),
        ({"environ": ["process_environment()"]}, "must be a qualified call"),
        (
            {
                "environ": ["lib.env.process_environment()"],
                "home": ["lib.env.process_environment()"],
            },
            "listed under more than one kind",
        ),
    ],
)
def test_a_reader_entry_is_a_qualified_call_under_one_kind(
    make_repo: RepoFactory, readers: dict[str, list[str]], fragment: str
) -> None:
    root = repo(make_repo, {}, readers)
    problems = [d.message for d in diagnostics(root, "policy-syntax")]
    assert len(problems) == 1
    assert fragment in problems[0]


def test_an_unknown_kind_is_not_a_section(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {}, {"weather": ["lib.env.process_environment()"]})
    assert any("no section 'weather'" in d.message for d in diagnostics(root, "policy-syntax"))


def test_a_reader_called_in_a_strict_directory_is_an_error_not_debt(make_repo: RepoFactory) -> None:
    root = repo(
        make_repo,
        {"app/mod.py": "from lib.env import process_environment\n\nx = process_environment()\n"},
        manifest='[purity]\n"mod.py" = "strict"\n',
    )
    found = [d for d in diagnostics(root, "ambient-read") if d.path == "app/mod.py"]
    assert [d.debt for d in found] == [None]
