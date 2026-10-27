from __future__ import annotations

from pathlib import Path

import pytest

from meizel.contracts.analysis import render_policy
from meizel.graph.model import Code
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures, run

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


def repo(make_repo: RepoFactory, files: dict[str, str], manifest: str = "") -> Path:
    return make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": POLICY,
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
        ("from pathlib import Path\n\nx = Path()\n", [("cwd", "pathlib.Path()")]),
        ("import time\n\nx = time.localtime()\n", [("clock", "time.localtime()")]),
        ("import time\n\nx = time.gmtime()\n", [("clock", "time.gmtime()")]),
        ("import time\n\nx = time.strftime('%H')\n", [("clock", "time.strftime()")]),
        ("import os\n\nx = os.urandom(8)\n", [("random", "os.urandom")]),
        ("import os\n\nx = os.getpgrp()\n", [("pid", "os.getpgrp")]),
        ("import threading\n\nx = threading.get_ident()\n", [("pid", "threading.get_ident")]),
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
        "import time\n\nx = time.localtime(0)\n",
        "import time\n\nx = time.gmtime(1.5)\n",
        "import time\n\nx = time.strftime('%H', parts)\n",
        "def f(thing):\n    return thing.expand()\n",
    ],
)
def test_the_same_calls_given_a_value_read_nothing(make_repo: RepoFactory, source: str) -> None:
    assert acquired(make_repo, source) == []


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
