from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meizel.contracts.analysis import render_policy
from meizel.graph.model import Code
from meizel.tests.synthetic import (
    RepoFactory,
    analysis_of,
    build_repo,
    diagnostics,
    failures,
    run,
)

ROOT = '[roles]\napplication = ["app", "cfg"]\ntooling = ["tool"]\ntests = ["tests"]\n'
POLICY = (
    render_policy({}, {})
    + '\n[ambient]\nsettings = ["settings", "get_field()"]\ndb = ["Database.from_settings()"]\n'
)
STATE = {
    "cfg/__init__.py": "settings = object()\n\n\ndef get_field(name: str) -> int:\n    return 1\n",
    "cfg/meizel.toml": '[state]\n"__init__.py" = ["settings"]\n',
    "tool/__init__.py": "",
    "tests/__init__.py": "",
}


def repo(
    make_repo: RepoFactory, files: dict[str, str], manifest: str = "", *, policy: str = POLICY
) -> Path:
    return make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": policy,
            "app/__init__.py": "",
            **STATE,
            "app/meizel.toml": manifest,
            **files,
        }
    )


# meiva:note
# The (kind, target) pairs the scanner finds in one file, whatever the rule judges.
def acquired(
    make_repo: RepoFactory, source: str, path: str = "app/mod.py"
) -> list[tuple[str, str]]:
    root = repo(make_repo, {path: source})
    return [(a.kind.value, a.target) for a in analysis_of(root).scans[path].ambient]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\n\nx = os.environ['A']\n", [("environ", "os.environ")]),
        ("import os\n\nx = os.environ.get('A')\n", [("environ", "os.environ")]),
        ("import os\n\nx = os.getenv('A')\n", [("environ", "os.getenv")]),
        ("from os import environ\n\nx = environ['A']\n", [("environ", "os.environ")]),
        ("import time\n\nx = time.time()\n", [("clock", "time.time")]),
        (
            "from datetime import datetime\n\nx = datetime.now()\n",
            [("clock", "datetime.datetime.now")],
        ),
        (
            "import datetime as dt\n\nx = dt.datetime.utcnow()\n",
            [("clock", "datetime.datetime.utcnow")],
        ),
        ("from pathlib import Path\n\nx = Path.home()\n", [("home", "pathlib.Path.home")]),
        ("import os\n\nx = os.getcwd()\n", [("cwd", "os.getcwd")]),
        ("import sys\n\nx = sys.platform\n", [("platform", "sys.platform")]),
        ("import platform\n\nx = platform.system()\n", [("platform", "platform.system")]),
        ("import os\n\nx = os.getpid()\n", [("pid", "os.getpid")]),
        ("import uuid\n\nx = uuid.uuid4()\n", [("random", "uuid.uuid4")]),
        ("import secrets\n\nx = secrets.token_hex(8)\n", [("random", "secrets.token_hex")]),
        ("import random\n\nx = random.random()\n", [("random", "random.random")]),
    ],
)
def test_the_standard_library_windows_onto_the_world_are_acquisitions(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import random\n\nx = random.Random(7)\n",
        "import time\n\nx = time.sleep\n",
        "from secrets import compare_digest\n\nx = compare_digest(b'a', b'b')\n",
        "import secrets as secure\n\nx = secure.compare_digest(b'a', b'b')\n",
        "import os\n\nx = os.path.join('a', 'b')\n",
        "x = 'os.environ'\n",
        "# os.environ in a comment\nx = 1\n",
        "def f(os):\n    return os.environ\n",
        "import uuid\n\nx = uuid.UUID(int=1)\n",
        "from datetime import datetime\n\nx = datetime(2026, 1, 1)\n",
    ],
)
def test_a_seeded_generator_a_value_or_a_look_alike_is_not(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import time\n\ndef f():\n    return time.monotonic()\n", []),
        ("import time\n\nDEADLINE = time.monotonic()\n", []),
        (
            "import time\n\ndef f():\n    time.sleep(1)\n    return time.monotonic()\n",
            [("clock", "time.monotonic")],
        ),
        (
            "import asyncio\nimport time\n\nasync def f():\n    await asyncio.sleep(1)\n"
            "    return time.perf_counter()\n",
            [("clock", "time.perf_counter")],
        ),
        (
            "import time\n\ndef f():\n    def inner():\n        time.sleep(1)\n    return time.monotonic()\n",
            [],
        ),
        (
            "import time\n\ndef f():\n    def inner():\n        return time.monotonic()\n"
            "    time.sleep(1)\n    return inner\n",
            [],
        ),
        ("import time\n\ndef f():\n    return time.time()\n", [("clock", "time.time")]),
    ],
)
def test_an_interval_clock_is_a_window_onto_the_world_only_where_the_function_sleeps(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import psutil\n\nx = psutil.Process()\n", [("pid", "psutil.Process()")]),
        ("import time\n\nx = time.localtime()\n", [("clock", "time.localtime()")]),
        ("import time\n\nx = time.gmtime()\n", [("clock", "time.gmtime()")]),
        ("import time\n\nx = time.strftime('%H')\n", [("clock", "time.strftime()")]),
        ("import os\n\nx = os.urandom(8)\n", [("random", "os.urandom")]),
        ("import os\n\nx = os.getpgrp()\n", [("pid", "os.getpgrp")]),
        ("import threading\n\nx = threading.get_ident()\n", [("pid", "threading.get_ident")]),
        (
            "import threading\n\nx = threading.get_native_id()\n",
            [("pid", "threading.get_native_id")],
        ),
        ("import time\n\nx = time.ctime()\n", [("clock", "time.ctime()")]),
        ("import time\n\nx = time.asctime()\n", [("clock", "time.asctime()")]),
        (
            "from pathlib import Path\n\nx = Path('~/a').expanduser()\n",
            [("home", "pathlib.Path.expanduser")],
        ),
        ("import os\n\nx = os.path.expanduser('~')\n", [("home", "os.path.expanduser")]),
    ],
)
def test_a_call_that_reads_the_process_or_the_clock_when_given_nothing_is_an_acquisition(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import psutil\n\nx = psutil.Process(7)\n",
        "from pathlib import Path\n\nx = Path('a')\n",
        "from pathlib import Path\n\nx = Path(*parts)\n",
        "from pathlib import Path\n\nx = Path()\n\nwhile d != Path():\n    d = d.parent\n",
        "import time\n\nx = time.localtime(0)\n",
        "import time\n\nx = time.gmtime(1.5)\n",
        "import time\n\nx = time.strftime('%H', parts)\n",
        "def f(thing):\n    return thing.expand()\n",
    ],
)
def test_the_same_calls_given_a_value_read_nothing(make_repo: RepoFactory, source: str) -> None:
    assert acquired(make_repo, source) == []


# meiva:note
# `expanduser()` spends a home read only when the path is the function's own: a `~` inside a value
# the caller named (a parameter, a local derived from one, a loop variable over one) is the
# caller's string, and expanding it asks the world for nothing the caller did not already name.
@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\n\ndef f(path):\n    return Path(path).expanduser()\n",
        "from pathlib import Path\n\ndef f(path):\n    p = Path(path)\n    return p.expanduser()\n",
        "from pathlib import Path\n\ndef f(path):\n    return Path(str(path)).expanduser()\n",
        "from pathlib import Path\n\ndef f(path):\n    return Path(f'{path}~').expanduser()\n",
        (
            "from pathlib import Path\n\ndef f(paths):\n"
            "    return [Path(p).expanduser() for p in paths]\n"
        ),
        (
            "from pathlib import Path\n\ndef f(items):\n    for p in items:\n"
            "        yield Path(p).expanduser()\n"
        ),
    ],
)
def test_expanding_a_path_the_caller_named_reads_nothing(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []


# meiva:note
# The read is still the function's own when nothing of the caller reaches the path: a literal, a
# module global, `sys.argv`, or a parameter the function then rebinds from a value it builds.
@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\n\ndef f():\n    return Path('~/a').expanduser()\n",
        "from pathlib import Path\n\nROOT = '~'\n\ndef f():\n    return Path(ROOT).expanduser()\n",
        (
            "import sys\nfrom pathlib import Path\n\ndef f():\n"
            "    return Path(sys.argv[1]).expanduser()\n"
        ),
        (
            "from pathlib import Path\n\ndef f(path):\n    p = Path(path)\n    q = p\n"
            "    return q.expanduser()\n"
        ),
        (
            "from pathlib import Path\n\ndef f(path):\n    path = Path('~')\n"
            "    return path.expanduser()\n"
        ),
    ],
)
def test_a_path_the_function_builds_itself_still_reads_the_home(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == [("home", "pathlib.Path.expanduser")]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import time\n\ndef _pause():\n    time.sleep(1)\n\n\ndef f():\n    _pause()\n"
            "    return time.monotonic()\n",
            [("clock", "time.monotonic")],
        ),
        (
            "import time\n\ndef _inner():\n    time.sleep(1)\n\n\ndef _outer():\n    _inner()\n\n\n"
            "def f():\n    _outer()\n    return time.monotonic()\n",
            [("clock", "time.monotonic")],
        ),
        (
            "import time\nfrom lib.backoff import retry_sleep\n\ndef f(n):\n    retry_sleep(n)\n"
            "    return time.monotonic()\n",
            [("clock", "time.monotonic")],
        ),
        (
            "import time\n\nasync def f(timers):\n    await timers.asleep(1)\n    return time.monotonic()\n",
            [("clock", "time.monotonic")],
        ),
        (
            "import time\n\ndef _pause():\n    time.sleep(1)\n\n\ndef f():\n    return time.monotonic()\n",
            [],
        ),
        (
            "import time\n\ndef _monotonic():\n    return time.monotonic()\n\n\n"
            "def wait():\n    time.sleep(1)\n    return _monotonic()\n",
            [("clock", "_monotonic()")],
        ),
        (
            "import time\n\ndef _monotonic():\n    return time.monotonic()\n\n\n"
            "def elapsed(start):\n    return _monotonic() - start\n",
            [],
        ),
        (
            "import asyncio\n\nasync def f():\n    await asyncio.sleep(1)\n"
            "    return asyncio.get_running_loop().time()\n",
            [("clock", "asyncio.loop.time")],
        ),
        (
            "import asyncio\n\nasync def f(loop):\n    await asyncio.sleep(1)\n    return loop.time()\n",
            [("clock", "asyncio.loop.time")],
        ),
        ("import asyncio\n\nasync def f():\n    return asyncio.get_running_loop().time()\n", []),
        (
            "import asyncio\n\nasync def f(loop):\n    await asyncio.sleep(1)\n    return loop.time\n",
            [],
        ),
        (
            "import time\n\nasync def f(waiter):\n    await waiter(1)\n    return time.monotonic()\n",
            [],
        ),
        (
            "import time\n\nasync def f(timers):\n    await timers.Sleep(1)\n    return time.monotonic()\n",
            [("clock", "time.monotonic")],
        ),
        (
            "import time\n\ndef monotonic():\n    return time.monotonic()\n\n\n"
            "def wait():\n    time.sleep(1)\n    return monotonic()\n",
            [("clock", "monotonic()")],
        ),
        (
            "import time\n\ndef _scaled(factor):\n    return time.monotonic()\n\n\n"
            "def wait():\n    time.sleep(1)\n    return _scaled(2)\n",
            [],
        ),
        (
            "import time\n\ndef _ms():\n    return int(time.monotonic() * 1000)\n\n\n"
            "def wait():\n    time.sleep(1)\n    return _ms()\n",
            [],
        ),
        (
            "import time\n\ndef _monotonic():\n    base = 0\n    return time.monotonic()\n\n\n"
            "def wait():\n    time.sleep(1)\n    return _monotonic()\n",
            [],
        ),
        (
            "import asyncio\n\nasync def f(event_loop):\n    await asyncio.sleep(1)\n"
            "    return event_loop.time()\n",
            [("clock", "asyncio.loop.time")],
        ),
        (
            "import asyncio\n\nasync def f(_loop):\n    await asyncio.sleep(1)\n    return _loop.time()\n",
            [("clock", "asyncio.loop.time")],
        ),
        (
            "import asyncio\n\nasync def f(loop):\n    await asyncio.sleep(1)\n    return loop.time(1)\n",
            [],
        ),
        (
            "import asyncio\n\nasync def f(sched):\n    await asyncio.sleep(1)\n    return sched.time()\n",
            [],
        ),
    ],
)
def test_a_wait_behind_a_name_a_helper_or_a_clock_wrapper_is_still_a_wait(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from cfg import settings\n\nx = settings.a\n", [("settings", "cfg.settings")]),
        ("import cfg\n\nx = cfg.settings.a\n", [("settings", "cfg.settings")]),
        ("from cfg import get_field\n\nx = get_field('a')\n", [("settings", "cfg.get_field")]),
        ("import cfg\n\nx = cfg.get_field('a')\n", [("settings", "cfg.get_field")]),
        ("from cfg import get_field\n\nx = get_field\n", []),
        (
            "from cfg import settings\n\nx = settings.a\ny = settings.b\n",
            [("settings", "cfg.settings")],
        ),
        (
            "from cfg import settings\n\ndef f(settings):\n    return settings.a\n",
            [("settings", "cfg.settings")],
        ),
    ],
)
def test_the_state_a_repository_keeps_is_acquired_where_it_is_imported_or_called(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


def test_a_name_is_state_only_in_a_module_that_declares_it(make_repo: RepoFactory) -> None:
    files = {
        "app/mod.py": "from app.other import settings\n\nx = settings.a\n",
        "app/other.py": "settings = 1\n",
    }
    root = repo(make_repo, files, '[uses]\n"mod.py" = ["other.py"]\n')
    assert analysis_of(root).scans["app/mod.py"].ambient == []


def test_a_test_file_acquires_nothing_the_rule_judges(make_repo: RepoFactory) -> None:
    root = repo(
        make_repo,
        {"app/test_x.py": "import os\n\ndef test_a():\n    assert os.environ['A']\n"},
    )
    assert analysis_of(root).scans["app/test_x.py"].ambient == []
    assert not diagnostics(root, "ambient-read")


def read_sites(make_repo: RepoFactory, source: str, manifest: str = "", path: str = "app/mod.py"):
    root = repo(make_repo, {path: source}, manifest)
    return root, [d for d in diagnostics(root, "ambient-read") if d.path == path]


@pytest.mark.parametrize(
    "source",
    [
        "import random\n\ndef f(rng: random.SystemRandom) -> random.SystemRandom:\n    return rng\n",
        "from random import SystemRandom\n\ndef f(rng: SystemRandom) -> None:\n    return None\n",
        "import random\n\nGENERATOR: random.SystemRandom | None = None\n",
        "import random\n\ndef f(x: object) -> bool:\n    return isinstance(x, random.SystemRandom)\n",
        "import random\nfrom typing import TypeAlias\n\nRng: TypeAlias = random.SystemRandom\n",
        "import random\n\ntype Rng = random.SystemRandom\n",
        "import random\nfrom typing import cast\n\ndef f(x: object):\n    return cast(random.SystemRandom, x)\n",
        "import os\n\ndef f(env: os._Environ[str]) -> None:\n    return None\n",
    ],
)
def test_a_name_that_only_describes_a_type_is_not_an_acquisition(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import os\nfrom typing import Annotated\n\ndef f(x: Annotated[str, os.getenv('A')]) -> None: ...\n",
            [("environ", "os.getenv")],
        ),
        (
            "import os\n\ndef f(x: object) -> bool:\n    return isinstance(x, os.getcwd())\n",
            [("cwd", "os.getcwd")],
        ),
        (
            "import os\nfrom typing import cast\n\ndef f(x: object):\n    return cast(os.getcwd(), x)\n",
            [("cwd", "os.getcwd")],
        ),
        (
            "import os\nfrom typing import TypeAlias\n\nHome: TypeAlias = os.getcwd()\n",
            [("cwd", "os.getcwd")],
        ),
    ],
)
def test_a_call_inside_a_type_position_is_still_a_value(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


def test_the_same_name_read_as_a_value_is_an_acquisition(make_repo: RepoFactory) -> None:
    source = "import random\n\nrng = random.SystemRandom()\n\ndef f(r: random.SystemRandom) -> None:\n    pass\n"
    assert acquired(make_repo, source) == [("random", "random.SystemRandom")]


def test_a_file_outside_a_root_is_told_where_the_world_is_read(make_repo: RepoFactory) -> None:
    _, sites = read_sites(make_repo, "import os\n\nx = os.environ['A']\n")
    assert [(d.line, d.debt.key if d.debt else None) for d in sites] == [
        (3, "mod.py#environ os.environ")
    ]
    assert "reads the environment" in sites[0].message
    assert [hint.split(":", 1)[0] for hint in sites[0].hints] == ["why", "instead", "see"]


def test_a_declared_root_may_read_its_kinds_and_only_those(make_repo: RepoFactory) -> None:
    manifest = '[roots]\n"mod.py" = { kinds = ["environ"], why = "reads the environment" }\n'
    source = "import os\nfrom cfg import settings\n\nx = os.environ['A']\ny = settings.a\n"
    _, sites = read_sites(make_repo, source, manifest)
    assert [d.debt.key for d in sites if d.debt] == ["mod.py#settings cfg.settings"]


def test_a_root_may_say_why(make_repo: RepoFactory) -> None:
    manifest = '[roots]\n"mod.py" = { kinds = ["environ"], why = "reads the launch environment" }\n'
    root, sites = read_sites(make_repo, "import os\n\nx = os.environ['A']\n", manifest)
    assert sites == []
    assert analysis_of(root).manifests["app"].roots["mod.py"].why == "reads the launch environment"


def test_a_root_kind_the_file_no_longer_acquires_is_stale(make_repo: RepoFactory) -> None:
    manifest = '[roots]\n"mod.py" = { kinds = ["environ", "settings"], why = "reads both" }\n'
    root, _ = read_sites(make_repo, "import os\n\nx = os.environ['A']\n", manifest)
    stale = diagnostics(root, "stale-root")
    assert [(d.path, "settings" in d.message) for d in stale] == [("app/meizel.toml", True)]


ROOT_AND_DEBT = (
    '[roots]\n"mod.py" = { kinds = ["environ"], why = "reads the environment" }\n'
    '\n[debt.ambient-read]\n"mod.py#environ os.environ" = 1\n'
)


# meiva:note
# `[roots]` and `[debt.ambient-read]` are the same declaration read twice: one read cannot be licensed
# to a file and paid for by it. The conflict is between two keys of one manifest, so it needs no
# source analysis.
def test_a_root_that_also_pays_for_its_read_is_a_conflict(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"app/mod.py": "import os\n\nx = os.environ['A']\n"}, ROOT_AND_DEBT)
    assert ("legal-read-recorded", "app/meizel.toml") in failures(root)


# meiva:note
# `debt-paid` needs a whole run to know a read is gone; this conflict does not, so `check --changed`
# reports it as well — the depth a pre-commit hook runs at.
def test_the_conflict_is_reported_in_a_changed_run_too(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, {"app/mod.py": "import os\n\nx = os.environ['A']\n"}, ROOT_AND_DEBT)
    capsys.readouterr()
    assert run(root, "check", "--changed", "app/mod.py") == 1
    assert "legal-read-recorded" in capsys.readouterr().err


# meiva:note
# Only the kind the root declares is legal: a root that pays for a *different* read is exactly what
# the account is for, and the conflict check must not swallow it.
def test_a_root_paying_for_another_kind_is_no_conflict(make_repo: RepoFactory) -> None:
    manifest = (
        '[roots]\n"mod.py" = { kinds = ["environ"], why = "reads the environment" }\n'
        '\n[debt.ambient-read]\n"mod.py#clock time.time" = 1\n'
    )
    source = "import os\nimport time\n\nx = os.environ['A']\ny = time.time()\n"
    root = repo(make_repo, {"app/mod.py": source}, manifest)
    assert "legal-read-recorded" not in {pair[0] for pair in failures(root)}
    assert "ambient-read" not in {pair[0] for pair in failures(root)}


# meiva:note
# The counter-proof partner: the same root with no entry pays nothing and says nothing.
def test_a_root_without_an_entry_is_no_conflict(make_repo: RepoFactory) -> None:
    manifest = ROOT_AND_DEBT.split("\n\n[debt.ambient-read]", maxsplit=1)[0] + "\n"
    root = repo(make_repo, {"app/mod.py": "import os\n\nx = os.environ['A']\n"}, manifest)
    assert "legal-read-recorded" not in {pair[0] for pair in failures(root)}


# meiva:note
# The account cannot carry this conflict: `legal-read-recorded` is not a debt code, so `record` refuses
# it, says so, exits 1, and leaves the manifest byte for byte as it was.
def test_the_conflict_cannot_be_recorded_as_debt(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, {"app/mod.py": "import os\n\nx = os.environ['A']\n"}, ROOT_AND_DEBT)
    before = (root / "app/meizel.toml").read_bytes()
    capsys.readouterr()
    assert run(root, "debt", "record", "app") == 1
    captured = capsys.readouterr()
    assert "cannot be recorded [legal-read-recorded]" in captured.err
    assert (root / "app/meizel.toml").read_bytes() == before


# meiva:note
# A root that licenses two kinds pays for neither of the reads it licenses: only the kind an entry
# names is a conflict, so the prefix must carry the kind and not stop at `name#`.
def test_only_the_kind_an_entry_names_is_a_conflict(make_repo: RepoFactory) -> None:
    manifest = (
        '[roots]\n"mod.py" = { kinds = ["environ", "clock"], why = "reads the world" }\n'
        '\n[debt.ambient-read]\n"mod.py#environ os.environ" = 1\n'
    )
    source = "import os\nimport time\n\nx = os.environ['A']\ny = time.time()\n"
    root = repo(make_repo, {"app/mod.py": source}, manifest)
    found = [d for d in failures(root) if d[0] == "legal-read-recorded"]
    assert found == [("legal-read-recorded", "app/meizel.toml")]


@pytest.mark.parametrize(
    ("manifest", "recordable"),
    [("", True), ('[purity]\n"mod.py" = "strict"\n', False)],
)
def test_a_read_is_recordable_debt_except_in_a_strict_file(
    make_repo: RepoFactory, manifest: str, recordable: bool
) -> None:
    _, sites = read_sites(make_repo, "import os\n\nx = os.environ['A']\n", manifest)
    assert [d.debt is not None for d in sites] == [recordable]


def test_a_recorded_read_passes_a_new_one_fails_and_a_paid_one_is_told_to_prune(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, {"app/mod.py": "import os\n\nx = os.environ['A']\n"})
    assert ("ambient-read", "app/mod.py") in failures(root)
    assert run(root, "debt", "record", ".") == 0
    assert not [pair for pair in failures(root) if pair[0] == "ambient-read"]
    (root / "app/mod.py").write_text(
        "import os\n\nx = os.environ['A']\ny = os.getenv('B')\n", encoding="utf-8"
    )
    assert ("ambient-read", "app/mod.py") in failures(root)
    (root / "app/mod.py").write_text("x = 1\n", encoding="utf-8")
    assert ("debt-paid", "app/meizel.toml") in failures(root)


def test_only_the_governed_roles_are_judged(make_repo: RepoFactory) -> None:
    files = {"other/__init__.py": "", "other/mod.py": "import os\n\nx = os.environ['A']\n"}
    root = make_repo(
        {
            "meizel.toml": '[roles]\napplication = ["app"]\nexamples = ["other"]\n',
            "meizel.policy.toml": POLICY,
            "app/__init__.py": "",
            **files,
        }
    )
    assert not diagnostics(root, "ambient-read")


def test_a_tooling_file_is_judged_like_the_application(make_repo: RepoFactory) -> None:
    _, sites = read_sites(make_repo, "import os\n\nx = os.environ['A']\n", path="tool/mod.py")
    assert [d.code for d in sites] == [Code.AMBIENT_READ]


def test_a_call_acquires_only_when_the_policy_marks_the_name_as_a_call(
    make_repo: RepoFactory,
) -> None:
    source = "from cfg import get_field\n\nx = get_field('a')\nreader = get_field\n"
    assert acquired(make_repo, source) == [("settings", "cfg.get_field")]


@pytest.mark.parametrize(
    ("source", "key"),
    [
        ("import time\n\nx = time.time()\n", "mod.py#clock time.time"),
        ("from pathlib import Path\n\nx = Path.home()\n", "mod.py#home pathlib.Path.home"),
        ("import os\n\nx = os.getcwd()\n", "mod.py#cwd os.getcwd"),
        ("import sys\n\nx = sys.platform\n", "mod.py#platform sys.platform"),
        ("import os\n\nx = os.getpid()\n", "mod.py#pid os.getpid"),
        ("import uuid\n\nx = uuid.uuid4()\n", "mod.py#random uuid.uuid4"),
        ("import os\n\nx = os.environ\n", "mod.py#environ os.environ"),
        ("from cfg import settings\n\nx = settings.a\n", "mod.py#settings cfg.settings"),
    ],
)
def test_every_kind_is_judged_outside_a_root(make_repo: RepoFactory, source: str, key: str) -> None:
    _, sites = read_sites(make_repo, source)
    assert [d.debt.key for d in sites if d.debt] == [key]


@pytest.mark.parametrize(
    "source",
    [
        "import random\n\nx = random.Random()\n",
        "import random\n\nx = random.Random(None)\n",
        "import random\n\nx = random.Random(x=None)\n",
        "from random import Random as Generator\n\nx = Generator()\n",
        "from random import Random as Generator\n\nx = Generator(x=None)\n",
        "import random\n\ndef f(seed):\n    return random.Random(seed)\n",
        "from random import Random as Generator\n\ndef f(seed):\n    return Generator(x=seed)\n",
        "import random\n\ndef f(inputs):\n    return random.Random(*inputs)\n",
        "import random\n\ndef f(inputs):\n    return random.Random(**inputs)\n",
    ],
)
def test_a_generator_without_a_proven_non_none_seed_acquires_randomness(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == [("random", "random.Random")]


@pytest.mark.parametrize(
    "source",
    [
        "import random\n\nx = random.Random(x=7)\n",
        "import random\n\nx = random.Random(0)\n",
        "import random\n\nx = random.Random('')\n",
        "import random\n\nx = random.Random(b'')\n",
        "from random import Random as Generator\n\nx = Generator(-7)\n",
        "import random as rng\n\nx = rng.Random(b'fixed')\n",
        "from random import Random\n\nx = Random\n",
    ],
)
def test_a_literal_seed_or_a_generator_type_reference_does_not_acquire_randomness(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []


# meiva:note
# A reader whose read a passed parameter spares, and a wrapper over it in the file that defines it.

IDENTITY = "def ensure(path: str, euid: int | None = None) -> str:\n    return path\n"
WRAPPER = (
    "def ensure(path: str, euid: int | None = None) -> str:\n"
    "    return path\n"
    "\n"
    "\n"
    "def wrapper(path: str) -> str:\n"
    "    return ensure(path)\n"
)


# meiva:note
# The policy tail that lists `name` as a reader whose read a passed `clause` spares.
def sparing(name: str, clause: str = "euid") -> str:
    entry = (
        f'{{ why = "reads the process identity unless the caller passes it", unless = "{clause}" }}'
    )
    return f'\n[ambient-readers.pid]\n"{name}" = {entry}\n'


def sparing_repo(make_repo: RepoFactory, files: dict[str, str], name: str, clause: str = "euid"):
    return repo(
        make_repo, {"cfg/identity.py": IDENTITY, **files}, policy=POLICY + sparing(name, clause)
    )


@pytest.mark.parametrize(
    "call",
    ["ensure('a', euid=0)", "ensure('a', 0)", "ensure('a', euid=os.geteuid())"],
)
def test_a_call_that_passes_the_parameter_does_not_acquire(
    make_repo: RepoFactory, call: str
) -> None:
    root = sparing_repo(
        make_repo,
        {"app/mod.py": f"import os\n\nfrom cfg.identity import ensure\n\nx = {call}\n"},
        "cfg.identity.ensure()",
    )
    found = {(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient}
    assert ("pid", "cfg.identity.ensure") not in found


def test_a_call_that_does_not_pass_the_parameter_acquires(make_repo: RepoFactory) -> None:
    root = sparing_repo(
        make_repo,
        {"app/mod.py": "from cfg.identity import ensure\n\nx = ensure('a')\n"},
        "cfg.identity.ensure()",
    )
    assert [(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient] == [
        ("pid", "cfg.identity.ensure")
    ]


def test_an_explicit_none_is_still_a_read(make_repo: RepoFactory) -> None:
    root = sparing_repo(
        make_repo,
        {"app/mod.py": "from cfg.identity import ensure\n\nx = ensure('a', euid=None)\n"},
        "cfg.identity.ensure()",
    )
    assert [(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient] == [
        ("pid", "cfg.identity.ensure")
    ]


def test_a_wrapper_beside_its_caller_acquires_where_it_is_called(make_repo: RepoFactory) -> None:
    root = sparing_repo(make_repo, {"app/mod.py": WRAPPER}, "app.mod.ensure()")
    assert [(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient] == [
        ("pid", "app.mod.ensure")
    ]


def test_a_wrapper_beside_its_caller_spares_the_read_when_it_passes_the_parameter(
    make_repo: RepoFactory,
) -> None:
    source = WRAPPER.replace("return ensure(path)", "return ensure(path, euid=0)")
    root = sparing_repo(make_repo, {"app/mod.py": source}, "app.mod.ensure()")
    assert analysis_of(root).scans["app/mod.py"].ambient == []


def test_the_same_file_layer_feeds_the_ambient_rule_alone(tmp_path: Path) -> None:
    files = {
        "meizel.toml": ROOT,
        "meizel.policy.toml": POLICY,
        "app/__init__.py": "",
        **STATE,
        "app/mod.py": WRAPPER,
    }
    for name in ("plain", "listed"):
        (tmp_path / name).mkdir()
    plain = build_repo(tmp_path / "plain", files)
    listed = build_repo(
        tmp_path / "listed", {**files, "meizel.policy.toml": POLICY + sparing("app.mod.ensure()")}
    )
    unread = replace(analysis_of(plain).scans["app/mod.py"], ambient=())
    read = replace(analysis_of(listed).scans["app/mod.py"], ambient=())
    assert unread == read
    assert analysis_of(listed).scans["app/mod.py"].ambient != ()


def test_a_clause_on_an_entry_that_is_not_a_call_is_a_policy_problem(
    make_repo: RepoFactory,
) -> None:
    root = repo(
        make_repo, {"app/mod.py": "x = 1\n"}, policy=POLICY + sparing("cfg.identity.ensure")
    )
    assert "policy-syntax" in {d.code.value for d in diagnostics(root, "policy-syntax")}


def test_a_clause_that_names_no_parameter_is_stale(make_repo: RepoFactory) -> None:
    root = sparing_repo(
        make_repo,
        {"app/mod.py": "from cfg.identity import ensure\n\nx = ensure('a')\n"},
        "cfg.identity.ensure()",
        clause="uid",
    )
    assert any("names no parameter 'uid'" in d.message for d in diagnostics(root, "stale-reader"))
