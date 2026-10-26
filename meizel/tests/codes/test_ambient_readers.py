from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from meizel.contracts.analysis import render_policy
from meizel.graph.model import MeizelError
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics

ROOT = '[roles]\napplication = ["app", "lib"]\ntests = ["tests"]\n'
READERS = {
    "environ": ["lib.env.process_environment()"],
    "home": ["lib.paths.meiva_home()", "lib.paths.Home.current()"],
}
CLOCKS = {
    "lib/clocks.py": (
        "class Clock:\n    @classmethod\n    def from_settings(cls):\n        return cls()\n\n\n"
        "SYSTEM = Clock()\n"
    ),
    "lib/meizel.toml": '[roots]\n"clocks.py" = ["clock"]\n',
}
FILES = {
    "lib/__init__.py": "",
    "lib/meizel.toml": (
        '[roots]\n"env.py" = { kinds = ["environ"], why = "reads it" }\n'
        '"paths.py" = { kinds = ["environ", "home"], why = "reads both" }\n'
    ),
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
        "lib/meizel.toml": '[roots]\n"env.py" = { kinds = ["environ"], why = "reads it" }\n',
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
        "lib/meizel.toml": '[roots]\n"env.py" = { kinds = ["environ"], why = "reads it" }\n',
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
    found = sites(
        make_repo, source, f'[roots]\n"mod.py" = {{ kinds = ["{kinds}"], why = "reads it" }}\n'
    )
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
        ("meizel.policy.toml", "'lib.env.vanished()' `vanished` is not defined in lib/env.py"),
        (
            "meizel.policy.toml",
            "'lib.gone.read()' names no module `lib.gone` and no class `gone` in lib/__init__.py",
        ),
        (
            "meizel.policy.toml",
            "'lib.paths.Home.vanished()' class `Home` in lib/paths.py defines no `vanished`",
        ),
    ]
    assert [d.line > 1 for d in stale] == [True, True, True]


@pytest.mark.parametrize(
    ("readers", "fragment"),
    [
        ({"environ": ["process_environment()"]}, "must be qualified"),
        ({"clock": ["SYSTEM"]}, "must be qualified"),
        (
            {
                "environ": ["lib.env.process_environment()"],
                "home": ["lib.env.process_environment()"],
            },
            "listed under more than one kind",
        ),
    ],
)
def test_a_reader_entry_is_qualified_and_listed_under_one_kind(
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


NAMES = {"clock": ["lib.clocks.SYSTEM", "lib.clocks.Clock()", "lib.clocks.Clock.from_settings()"]}


def clock_acquired(make_repo: RepoFactory, source: str, extra: dict[str, str] | None = None):
    root = repo(make_repo, {**CLOCKS, "app/mod.py": source, **(extra or {})}, NAMES)
    return [(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from lib.clocks import SYSTEM\n\nx = SYSTEM.now()\n", [("clock", "lib.clocks.SYSTEM")]),
        ("import lib.clocks as c\n\nx = c.SYSTEM.now()\n", [("clock", "lib.clocks.SYSTEM")]),
        ("from lib import clocks\n\nx = clocks.SYSTEM\n", [("clock", "lib.clocks.SYSTEM")]),
        ("from lib.clocks import Clock\n\nx = Clock()\n", [("clock", "lib.clocks.Clock")]),
        (
            "from lib.clocks import Clock\n\nx = Clock.from_settings()\n",
            [("clock", "lib.clocks.Clock.from_settings")],
        ),
        ("from lib.clocks import Clock\n\ndef f(c: Clock) -> Clock:\n    return c\n", []),
        ("def SYSTEM():\n    return 1\n\n\nx = SYSTEM()\n", []),
    ],
)
def test_a_bare_name_acquires_where_it_is_imported_or_read_and_a_class_where_it_is_built(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert clock_acquired(make_repo, source) == expected


def test_a_bare_name_is_followed_through_a_re_export(make_repo: RepoFactory) -> None:
    extra = {"lib/__init__.py": "from lib.clocks import SYSTEM\n"}
    found = clock_acquired(make_repo, "import lib\n\nx = lib.SYSTEM.now()\n", extra)
    assert found == [("clock", "lib.SYSTEM")]


def test_a_function_that_moved_and_left_a_re_export_is_stale(make_repo: RepoFactory) -> None:
    files = {
        "lib/impl.py": "import os\n\n\ndef process_environment():\n    return os.environ\n",
        "lib/env.py": "from lib.impl import process_environment\n",
        "lib/meizel.toml": '[roots]\n"impl.py" = ["environ"]\n',
        "app/mod.py": "from lib.impl import process_environment\n\nx = process_environment()\n",
    }
    root = repo(make_repo, files, {"environ": ["lib.env.process_environment()"]})
    stale = diagnostics(root, "stale-reader")
    assert [d.message.split("] ", 1)[1] for d in stale] == [
        "'lib.env.process_environment()' `process_environment` is imported by lib/env.py, "
        "not defined there; list the module that defines it"
    ]


def test_a_value_listed_as_a_call_and_a_function_listed_as_a_name_are_stale(
    make_repo: RepoFactory,
) -> None:
    files = {"lib/clocks.py": "SYSTEM = object()\n"}
    readers = {
        "environ": ["lib.env.process_environment"],
        "clock": ["lib.clocks.SYSTEM()"],
        "home": ["lib.paths.Home.current"],
    }
    stale = diagnostics(repo(make_repo, files, readers), "stale-reader")
    assert sorted(d.message.split("] ", 1)[1] for d in stale) == [
        "'lib.clocks.SYSTEM()' `SYSTEM` is a value, not a callable: list it as `lib.clocks.SYSTEM`, without `()`",
        "'lib.env.process_environment' `process_environment` is a function: list it as `lib.env.process_environment()`",
        "'lib.paths.Home.current' `Home.current` is a method: list it as `lib.paths.Home.current()`",
    ]


def test_a_class_may_be_listed_as_a_name_and_as_a_call(make_repo: RepoFactory) -> None:
    files = {"lib/clocks.py": "class Clock:\n    pass\n"}
    readers = {"clock": ["lib.clocks.Clock", "lib.clocks.Clock()"]}
    root = repo(make_repo, files, readers)
    assert diagnostics(root, "stale-reader") == []
    assert diagnostics(root, "policy-syntax") == []


# meiva:note
# A reader defined in `z00` and imported on down the chain, each module sorting before the one it
# imports from, so that each round of the scan can follow only one hop.
def chain(length: int) -> dict[str, str]:
    names = [f"{chr(ord('z') - step)}{step:02d}" for step in range(length)]
    files = {"lib/z00.py": "import os\n\n\ndef read():\n    return os.environ\n"}
    for before, after in pairwise(names):
        files[f"lib/{after}.py"] = f"from lib.{before} import read\n"
    files["lib/meizel.toml"] = '[roots]\n"z00.py" = ["environ"]\n'
    files["app/mod.py"] = f"from lib.{names[-1]} import read\n\nx = read()\n"
    return files


def test_a_call_imported_through_several_re_exports_is_still_the_reader(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, chain(5), {"environ": ["lib.z00.read()"]})
    scan = analysis_of(root).scans["app/mod.py"]
    assert [a.kind.value for a in scan.ambient] == ["environ"]


def test_a_chain_of_re_exports_too_long_to_follow_fails_instead_of_going_unseen(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, chain(16), {"environ": ["lib.z00.read()"]})
    with pytest.raises(MeizelError, match="more than 12 modules"):
        analysis_of(root)


def test_a_class_re_exported_by_a_relative_import_is_followed(make_repo: RepoFactory) -> None:
    files = {
        "lib/__init__.py": "from .paths import Home\n",
        "app/mod.py": "from lib import Home\n\nx = Home.current()\n",
    }
    root = repo(make_repo, files)
    scan = analysis_of(root).scans["app/mod.py"]
    assert [(a.kind.value, a.target) for a in scan.ambient] == [("home", "lib.Home.current")]
