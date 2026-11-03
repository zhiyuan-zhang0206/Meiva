from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures, record, run

ROOT = '[roles]\napplication = ["app"]\ntests = ["tests"]\n'


def repo(make_repo: RepoFactory, files: dict[str, str]) -> Path:
    return make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": render_policy({}, {}, {}),
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
        ("import getpass\n\nx = getpass.getpass()\n", [("pid", "getpass.getpass")]),
        ("import pwd\nimport os\n\nx = pwd.getpwnam('a')\n", [("pid", "pwd.getpwnam")]),
        ("import os\n\nx = os.getlogin()\n", [("pid", "os.getlogin")]),
        ("import tempfile\n\nx = tempfile.gettempdir()\n", [("environ", "tempfile.gettempdir")]),
        ("import os\n\nx = os.path.expandvars('$A')\n", [("environ", "os.path.expandvars")]),
        ("import os\n\nx = os.get_exec_path()\n", [("environ", "os.get_exec_path")]),
        ("import os\n\nx = os.path.abspath('a')\n", [("cwd", "os.path.abspath")]),
        ("import os\n\nx = os.cpu_count()\n", [("platform", "os.cpu_count")]),
        ("import os\n\nx = os.getloadavg()\n", [("platform", "os.getloadavg")]),
        (
            "import uuid\n\nx = uuid.getnode()\n",
            [("platform", "uuid.getnode"), ("random", "uuid.getnode")],
        ),
        ("import time\n\nx = time.tzname\n", [("clock", "time.tzname")]),
        ("import time\n\nx = time.mktime(parts)\n", [("clock", "time.mktime")]),
        ("import time\n\nx = time.localtime(0)\n", [("clock", "time.localtime")]),
        ("answer = input('go? ')\n", [("pid", "builtins.input")]),
        (
            "import site\n\nx = site.getusersitepackages()\n",
            [("environ", "site.getusersitepackages"), ("home", "site.getusersitepackages")],
        ),
        (
            "import email.utils\n\nx = email.utils.localtime()\n",
            [("clock", "email.utils.localtime")],
        ),
        (
            "import email.utils\n\nx = email.utils.make_msgid()\n",
            [
                ("clock", "email.utils.make_msgid"),
                ("pid", "email.utils.make_msgid"),
                ("platform", "email.utils.make_msgid"),
                ("random", "email.utils.make_msgid"),
            ],
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
        (
            "import netrc\n\nx = netrc.netrc()\n",
            [("home", "netrc.netrc()"), ("pid", "netrc.netrc()")],
        ),
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


# meiva:note
# A call that reads several channels is one acquisition for each of them, at the same line and under
# the same target, so that the debt of a file lists every channel its call reads and a root that
# declares one of them does not hide the others.
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import getpass\n\nx = getpass.getuser()\n",
            [("environ", "getpass.getuser"), ("pid", "getpass.getuser")],
        ),
        (
            "import shutil\n\nx = shutil.get_terminal_size()\n",
            [("environ", "shutil.get_terminal_size"), ("pid", "shutil.get_terminal_size")],
        ),
        (
            "import uuid\n\nx = uuid.uuid1()\n",
            [("clock", "uuid.uuid1"), ("platform", "uuid.uuid1"), ("random", "uuid.uuid1")],
        ),
        ("import uuid\n\nx = uuid.uuid4()\n", [("random", "uuid.uuid4")]),
        (
            "import sysconfig\n\nx = sysconfig.get_paths()\n",
            [
                ("environ", "sysconfig.get_paths"),
                ("home", "sysconfig.get_paths"),
                ("pid", "sysconfig.get_paths"),
            ],
        ),
        (
            "import site\n\nx = site.check_enableusersite()\n",
            [
                ("argv", "site.check_enableusersite"),
                ("environ", "site.check_enableusersite"),
                ("pid", "site.check_enableusersite"),
            ],
        ),
        (
            "from tempfile import mkstemp\n\nx = mkstemp()\n",
            [("environ", "tempfile.mkstemp()"), ("random", "tempfile.mkstemp()")],
        ),
        (
            "import ctypes.util\n\nx = ctypes.util.find_library('c')\n",
            [("environ", "ctypes.util.find_library")],
        ),
        ("import webbrowser\n\nwebbrowser.open('x')\n", [("environ", "webbrowser.open")]),
    ],
)
def test_a_call_that_reads_several_channels_acquires_each_of_them(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(make_repo, source) == expected


# meiva:note
# A clause spares the call as a whole: `mkdtemp(dir=...)` still draws a random name and records
# nothing, and `make_msgid(domain=...)` has no clause because it would spare the clock, the process
# id and the random number with the host name. These are pinned so that the documentation of the
# first stays true.
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import tempfile\n\nx = tempfile.mkdtemp(dir='/a')\n", []),
        ("import fileinput\n\nx = fileinput.input(files=['/a'])\n", []),
        (
            "import email.utils\n\nx = email.utils.make_msgid(domain='a')\n",
            [
                ("clock", "email.utils.make_msgid"),
                ("pid", "email.utils.make_msgid"),
                ("platform", "email.utils.make_msgid"),
                ("random", "email.utils.make_msgid"),
            ],
        ),
        ("import tempfile\n\nx = tempfile.mkdtemp\n", []),
    ],
)
def test_a_clause_spares_the_whole_call_and_a_bare_reference_acquires_nothing(
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
        (
            "import tempfile\n\nx = tempfile.mkdtemp()\n",
            [("environ", "tempfile.mkdtemp()"), ("random", "tempfile.mkdtemp()")],
        ),
        (
            "import tempfile\n\nx = tempfile.NamedTemporaryFile('w')\n",
            [
                ("environ", "tempfile.NamedTemporaryFile()"),
                ("random", "tempfile.NamedTemporaryFile()"),
            ],
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
        (
            "import fileinput\n\nx = fileinput.input()\n",
            [("argv", "fileinput.input()"), ("pid", "fileinput.input()")],
        ),
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
# `parse_args(argv)` in `def main(argv=None)` is `parse_args()` hidden behind a parameter: argparse
# reads `sys.argv[1:]` when it is handed `None`. The call is an `argv` acquisition when the first
# argument is a parameter of the same function that defaults to `None` and that the body does not
# rebind. Facts of the one function only: nothing is asked of its callers.
@pytest.mark.parametrize(
    "source",
    [
        "def main(argv=None):\n    return parser.parse_args(argv)\n",
        "def main(argv: list[str] | None = None):\n    return parser.parse_args(argv)\n",
        "def main(argv=None):\n    return parser.parse_args(args=argv)\n",
        "def main(*, argv=None):\n    return parser.parse_args(argv)\n",
        "def main(a, argv=None, b=1):\n    return parser.parse_args(argv)\n",
        "def main(argv=None):\n    return build().parse_args(argv)\n",
        "class C:\n    def main(self, argv=None):\n        return self.parser.parse_args(argv)\n",
    ],
)
def test_an_optional_parameter_handed_to_a_parser_is_the_command_line(
    make_repo: RepoFactory, source: str
) -> None:
    assert acquired(make_repo, source) == [("argv", "argparse.ArgumentParser.parse_args")]


def test_the_optional_parameter_of_another_parser_method_is_the_command_line(
    make_repo: RepoFactory,
) -> None:
    source = "def main(argv=None):\n    return parser.parse_known_args(argv)\n"
    assert acquired(make_repo, source) == [("argv", "argparse.ArgumentParser.parse_known_args")]


@pytest.mark.parametrize(
    "source",
    [
        "def main(argv: list[str]):\n    return parser.parse_args(argv)\n",
        "def main(argv=['a']):\n    return parser.parse_args(argv)\n",
        "def main(argv=DEFAULT):\n    return parser.parse_args(argv)\n",
        "def main(argv=None):\n    argv = [] if argv is None else argv\n"
        "    return parser.parse_args(argv)\n",
        "def main(argv=None):\n    if argv is None:\n        argv = []\n"
        "    return parser.parse_args(argv)\n",
        "def main(argv=None):\n    return parser.parse_args(argv or [])\n",
        "def main(argv=None, other=None):\n    return parser.parse_args(other_list)\n",
        "def main(argv=None):\n    def inner(argv):\n        return parser.parse_args(argv)\n"
        "    return inner\n",
        "def main(argv=None):\n    return parser.format_help(argv)\n",
        "def main(moment=None):\n    return other.astimezone(moment)\n",
    ],
)
def test_a_parameter_that_is_required_or_decided_before_the_parser_is_not_the_command_line(
    make_repo: RepoFactory, source: str
) -> None:
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


# meiva:note
# One call that reads four channels (the clock, the process id, entropy, the host name) is four
# reads at the same line, so what a root does not declare is still told, kind by kind.
_MSGID = "import email.utils\n\nx = email.utils.make_msgid()\n"
_ALL_FOUR = {
    f"mod.py#{kind} email.utils.make_msgid" for kind in ("clock", "pid", "platform", "random")
}
_RANDOM_ROOT = '\n[roots]\n"mod.py" = { kinds = ["random"], why = "draws the message id" }\n'


def _recorded_keys(root: Path) -> dict[str, int]:
    table = tomllib.loads((root / "app/meizel.toml").read_text(encoding="utf-8"))
    return {key: entry["count"] for key, entry in table["debt"]["ambient-read"].items()}


def test_a_root_that_declares_part_of_a_multiple_kind_call_is_told_of_the_rest(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, {"app/mod.py": _MSGID, "app/meizel.toml": _RANDOM_ROOT.lstrip()})
    sites = diagnostics(root, "ambient-read")
    assert {d.debt.key for d in sites if d.debt} == _ALL_FOUR - {
        "mod.py#random email.utils.make_msgid"
    }
    assert {d.line for d in sites} == {3}


def test_a_multiple_kind_call_is_recorded_as_one_debt_key_per_kind(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"app/mod.py": _MSGID})
    assert record(root, ".") == 0
    assert _recorded_keys(root) == dict.fromkeys(_ALL_FOUR, 1)
    assert not [pair for pair in failures(root) if pair[0] == "ambient-read"]


def test_pruning_drops_only_the_kind_of_a_multiple_kind_call_that_was_paid(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, {"app/mod.py": _MSGID})
    assert record(root, ".") == 0
    manifest = root / "app/meizel.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + _RANDOM_ROOT, encoding="utf-8")
    assert ("debt-paid", "app/meizel.toml") in failures(root)
    assert run(root, "debt", "prune") == 0
    assert set(_recorded_keys(root)) == _ALL_FOUR - {"mod.py#random email.utils.make_msgid"}
