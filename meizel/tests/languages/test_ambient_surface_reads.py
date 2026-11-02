from __future__ import annotations

from pathlib import Path

import pytest

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures

ROOT = '[roles]\napplication = ["app"]\ntests = ["tests"]\n'


def repo(make_repo: RepoFactory, files: dict[str, str]) -> Path:
    return make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": render_policy({}, {}),
            "app/__init__.py": "",
            "app/meizel.toml": "",
            "tests/__init__.py": "",
            **files,
        }
    )


# meiva:note
# The (kind, target) pairs the scanner finds in one file, whatever the rule judges.
def acquired(
    make_repo: RepoFactory, source: str, path: str = "app/mod.py"
) -> list[tuple[str, str]]:
    root = repo(make_repo, {path: source})
    return [(a.kind, a.target) for a in analysis_of(root).scans[path].ambient]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import sys\n\nx = sys.argv[1:]\n", [("argv", "sys.argv")]),
        ("from sys import orig_argv\n\nx = orig_argv\n", [("argv", "sys.orig_argv")]),
        ("import sys\n\nx = sys.executable\n", [("pid", "sys.executable")]),
        ("import sys\n\nx = sys.stdout.isatty()\n", [("pid", "sys.stdout.isatty")]),
        ("import getpass\n\nx = getpass.getuser()\n", [("pid", "getpass.getuser")]),
        ("import pwd\nimport os\n\nx = pwd.getpwnam('a')\n", [("pid", "pwd.getpwnam")]),
        ("import os\n\nx = os.getlogin()\n", [("pid", "os.getlogin")]),
        ("import tempfile\n\nx = tempfile.gettempdir()\n", [("environ", "tempfile.gettempdir")]),
        ("import os\n\nx = os.path.expandvars('$A')\n", [("environ", "os.path.expandvars")]),
        ("import os\n\nx = os.get_exec_path()\n", [("environ", "os.get_exec_path")]),
        ("import os\n\nx = os.path.abspath('a')\n", [("cwd", "os.path.abspath")]),
        ("import os\n\nx = os.cpu_count()\n", [("platform", "os.cpu_count")]),
        ("import os\n\nx = os.getloadavg()\n", [("platform", "os.getloadavg")]),
        ("import uuid\n\nx = uuid.getnode()\n", [("platform", "uuid.getnode")]),
        ("import time\n\nx = time.tzname\n", [("clock", "time.tzname")]),
        ("import time\n\nx = time.mktime(parts)\n", [("clock", "time.mktime")]),
        ("import time\n\nx = time.localtime(0)\n", [("clock", "time.localtime")]),
        ("answer = input('go? ')\n", [("pid", "builtins.input")]),
        ("import site\n\nx = site.getusersitepackages()\n", [("home", "site.getusersitepackages")]),
        (
            "import email.utils\n\nx = email.utils.localtime()\n",
            [("clock", "email.utils.localtime")],
        ),
        (
            "import email.utils\n\nx = email.utils.make_msgid()\n",
            [("random", "email.utils.make_msgid")],
        ),
        ("import time\n\nx = time.get_clock_info('time')\n", [("platform", "time.get_clock_info")]),
        (
            "import email.utils\n\nx = email.utils.formatdate(1.5, localtime=True)\n",
            [("clock", "email.utils.formatdate")],
        ),
        # meiva:note
        # Over-recorded on purpose: the table cannot say "given a time and not `localtime`".
        (
            "import email.utils\n\nx = email.utils.formatdate(1.5)\n",
            [("clock", "email.utils.formatdate")],
        ),
        ("import netrc\n\nx = netrc.netrc()\n", [("home", "netrc.netrc()")]),
        (
            "import ssl\n\nx = ssl.get_default_verify_paths()\n",
            [("environ", "ssl.get_default_verify_paths")],
        ),
        (
            "import ssl\n\nx = ssl.create_default_context()\n",
            [("environ", "ssl.create_default_context()")],
        ),
        ("import ssl\n\nx = ssl.RAND_bytes(8)\n", [("random", "ssl.RAND_bytes")]),
    ],
)
def test_the_channels_the_table_judges_are_acquisitions(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import os\n\nx = os.sep\n",
        "import sys\n\nx = sys.version_info\n",
        "import sys\n\nsys.path.insert(0, 'a')\nsys.stdout.write('a')\n",
        "from pathlib import Path\n\nx = Path('/a').resolve()\n",
        "import os\n\nx = os.path.realpath('/a')\n",
        "import tempfile\n\nx = tempfile.gettempprefix()\n",
        "import time\n\nx = time.gmtime(1.5)\n",
    ],
)
def test_a_name_that_reads_nothing_is_not_an_acquisition(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import tempfile\n\nx = tempfile.mkdtemp()\n", [("environ", "tempfile.mkdtemp()")]),
        (
            "import tempfile\n\nx = tempfile.NamedTemporaryFile('w')\n",
            [("environ", "tempfile.NamedTemporaryFile()")],
        ),
        ("import shutil\n\nx = shutil.which('git')\n", [("environ", "shutil.which()")]),
        ("import os\n\nx = os.listdir()\n", [("cwd", "os.listdir()")]),
        ("import os\n\nx = os.path.relpath('a')\n", [("cwd", "os.path.relpath()")]),
        (
            "from datetime import datetime\n\nx = datetime.fromtimestamp(1.5)\n",
            [("clock", "datetime.datetime.fromtimestamp()")],
        ),
        (
            "def f(moment):\n    return moment.astimezone()\n",
            [("clock", "datetime.datetime.astimezone")],
        ),
        (
            "import argparse\n\nx = argparse.ArgumentParser().parse_args()\n",
            [("argv", "argparse.ArgumentParser.parse_args")],
        ),
        (
            "def f(parser):\n    return parser.parse_known_args(namespace=None)\n",
            [("argv", "argparse.ArgumentParser.parse_known_args")],
        ),
        ("import fileinput\n\nx = fileinput.input()\n", [("argv", "fileinput.input()")]),
        (
            "import email.utils\n\nx = email.utils.formatdate(localtime=True)\n",
            [("clock", "email.utils.formatdate")],
        ),
        ("def f(path):\n    return path.absolute()\n", []),
        ("def f():\n    return build().absolute()\n", [("cwd", "pathlib.Path.absolute")]),
    ],
)
def test_a_call_that_is_not_given_the_value_that_spares_its_read_acquires(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import tempfile\n\nx = tempfile.mkdtemp(dir='/a')\n",
        "import tempfile\n\nx = tempfile.mkdtemp(None, None, '/a')\n",
        "import tempfile\n\nx = tempfile.NamedTemporaryFile('w', dir='/a')\n",
        "import shutil\n\nx = shutil.which('git', path='/bin')\n",
        "import os\n\nx = os.listdir('/a')\n",
        "import os\n\nx = os.path.relpath('a', '/b')\n",
        "from datetime import UTC, datetime\n\nx = datetime.fromtimestamp(1.5, UTC)\n",
        "from datetime import UTC, datetime\n\nx = datetime.fromtimestamp(1.5, tz=UTC)\n",
        "def f(moment, zone):\n    return moment.astimezone(zone)\n",
        "def f(parser, argv):\n    return parser.parse_args(argv)\n",
        "def f(parser, argv):\n    return parser.parse_args(args=argv)\n",
        "import fileinput\n\nx = fileinput.input(files=['/a'])\n",
        "import fileinput\n\nx = fileinput.FileInput(['/a'])\n",
        "import netrc\n\nx = netrc.netrc('/a')\n",
        "import ssl\n\nx = ssl.create_default_context(cafile='/a')\n",
    ],
)
def test_the_same_calls_given_the_value_read_nothing(make_repo: RepoFactory, source: str) -> None:
    assert acquired(make_repo, source) == []


# meiva:note
# `input` is the builtin only when the file does not bind the name: a parameter, an assignment or
# a comprehension variable called `input` is the file's own value and reads no stream.
@pytest.mark.parametrize(
    "source",
    [
        "def f(input):\n    return input('x')\n",
        "input = lambda prompt: prompt\n\nx = input('x')\n",
        "x = [input for input in ['a']]\n",
    ],
)
def test_an_input_the_file_binds_is_not_the_builtin(make_repo: RepoFactory, source: str) -> None:
    assert acquired(make_repo, source) == []


def test_a_path_from_the_command_line_reads_both_the_command_line_and_the_home(
    make_repo: RepoFactory,
) -> None:
    source = (
        "import sys\nfrom pathlib import Path\n\ndef f():\n"
        "    return Path(sys.argv[1]).expanduser()\n"
    )
    assert acquired(make_repo, source) == [
        ("argv", "sys.argv"),
        ("home", "pathlib.Path.expanduser"),
    ]


# meiva:note
# A member of a channel module that the ambient surface does not judge is a plain error: it cannot be
# recorded as debt, because the gap is in the vocabulary and not in the file, and the owner of the
# gap is the table.
@pytest.mark.parametrize(
    "source",
    [
        "import os\n\nx = os.brand_new_function()\n",
        "from sys import brand_new\n\nx = brand_new\n",
        "import os.path\n\nx = os.path.brand_new('a')\n",
    ],
)
def test_an_unjudged_member_of_a_channel_module_fails_and_cannot_be_recorded(
    make_repo: RepoFactory, source: str
) -> None:
    root = repo(make_repo, {"app/mod.py": source})
    found = diagnostics(root, "ambient-unjudged")
    assert [d.path for d in found] == ["app/mod.py"]
    assert found[0].debt is None
    assert ("ambient-unjudged", "app/mod.py") in failures(root)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("app/test_x.py", "import os\n\ndef test_a():\n    assert os.brand_new\n"),
        ("app/mod.py", "import os\n\ndef f(env: os.Brand) -> None:\n    return None\n"),
        ("app/mod.py", "import os\n\nx = os._exit\n"),
        ("app/mod.py", "import json\n\nx = json.brand_new\n"),
    ],
)
def test_a_test_file_a_type_position_a_private_name_or_another_module_is_not_unjudged(
    make_repo: RepoFactory, path: str, source: str
) -> None:
    root = repo(make_repo, {path: source})
    assert not diagnostics(root, "ambient-unjudged")


# meiva:note
# A known blind spot, pinned so that the documentation of it stays true: the analysis has no types,
# so it cannot tell a relative receiver from an absolute one, and `resolve()` and `realpath` of a
# path that came from the command line anchor it at the working directory with no record.
@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\n\nx = Path('').resolve()\n",
        "import os\n\nx = os.path.realpath('.')\n",
        "from pathlib import Path\n\ndef f(argv):\n    return [Path(a).resolve() for a in argv]\n",
    ],
)
def test_resolving_a_relative_path_reads_the_working_directory_unrecorded(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == []
