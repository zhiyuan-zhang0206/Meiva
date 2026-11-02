from __future__ import annotations

import ast
import builtins
import datetime
import email.utils
import fileinput
import fnmatch
import getpass
import grp
import inspect
import locale
import multiprocessing
import os
import os.path as os_path
import pathlib
import platform
import pwd
import random
import resource
import secrets
import shutil
import site
import socket
import sys
import sysconfig
import tempfile
import threading
import time
import types
import uuid
import zoneinfo
from collections import Counter
from pathlib import Path

import basedpyright
import pytest

from meizel.graph.ambient_surface import (
    CHANNEL_MODULES,
    INTERVAL_CLOCKS,
    METHOD_READS,
    groups,
    judged,
    unjudged,
)
from meizel.graph.model import AmbientKind

# meiva:note
# The channel modules as objects, spelled with static imports.
_MODULES: dict[str, types.ModuleType] = {
    "datetime": datetime,
    "email.utils": email.utils,
    "fileinput": fileinput,
    "getpass": getpass,
    "grp": grp,
    "locale": locale,
    "multiprocessing": multiprocessing,
    "os": os,
    "os.path": os_path,
    "pathlib": pathlib,
    "platform": platform,
    "pwd": pwd,
    "random": random,
    "resource": resource,
    "secrets": secrets,
    "shutil": shutil,
    "site": site,
    "socket": socket,
    "sys": sys,
    "sysconfig": sysconfig,
    "tempfile": tempfile,
    "threading": threading,
    "time": time,
    "uuid": uuid,
    "zoneinfo": zoneinfo,
}


# meiva:note
# The typeshed that the type checker ships: it lists the names of every platform and of the
# interpreter versions the live module on this machine does not have.
_TYPESHED = Path(basedpyright.__file__).parent / "dist" / "typeshed-fallback" / "stdlib"
_STUBBED = {"os.path": "posixpath"}


def _stub_names(module: str) -> set[str]:
    stem = _STUBBED.get(module, module).replace(".", "/")
    path = _TYPESHED / f"{stem}.pyi"
    if not path.is_file():
        path = _TYPESHED / stem / "__init__.pyi"
    found: set[str] = set()

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                found.add(node.name)
            elif isinstance(node, ast.Assign):
                found.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                found.update(a.name for a in node.names if a.asname == a.name)
            elif isinstance(node, ast.If):
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, ast.Try):
                walk(node.body)

    walk(ast.parse(path.read_text()).body)
    return found


# meiva:note
# The public names of a channel module: the live module's and the stub's, without the submodules
# (`os.path` is judged as a module of its own) and the fallbacks a platform leaves as `None`.
def _public_names(module: str) -> set[str]:
    live = _MODULES[module]
    names = {
        name
        for name in dir(live)
        if not inspect.ismodule(getattr(live, name)) and getattr(live, name) is not None
    }
    names |= _stub_names(module)
    return {
        name
        for name in names
        if not name.startswith("_") and f"{module}.{name}" not in CHANNEL_MODULES
    }


def test_every_channel_module_is_spelled_in_this_file() -> None:
    assert set(_MODULES) == CHANNEL_MODULES


def test_the_interpreter_the_table_was_judged_on_is_the_supported_runtime() -> None:
    assert sys.version_info[:2] == (3, 12)


@pytest.mark.parametrize("module", sorted(CHANNEL_MODULES))
def test_every_public_name_of_a_channel_module_is_judged(module: str) -> None:
    unlisted = sorted(
        name
        for name in _public_names(module)
        if (reading := judged(f"{module}.{name}")) is None or reading.name != f"{module}.{name}"
    )
    assert not unlisted, (
        f"{module}: judge these names in meizel/graph/ambient_surface.py "
        f"(a kind, or None with the reason it is not a read): {unlisted}"
    )


def test_a_name_is_judged_by_one_group_only() -> None:
    seen = Counter(f"{group.module}.{name}" for group in groups() for name in group.names)
    assert [name for name, count in seen.items() if count > 1] == []


def test_every_group_says_why() -> None:
    assert [g.module for g in groups() if len(g.judgment.why.split()) < 3] == []


def test_a_pattern_names_something_and_an_entry_names_a_name_that_exists() -> None:
    stale: list[str] = []
    for group in groups():
        if group.module not in CHANNEL_MODULES:
            continue
        existing = _public_names(group.module)
        for name in group.names:
            head = name.split(".")[0]
            if "*" in name:
                if not fnmatch.filter(sorted(existing), name):
                    stale.append(f"{group.module}.{name}")
            elif head not in existing and not head.startswith("_"):
                stale.append(f"{group.module}.{name}")
    assert stale == []


def test_a_member_of_a_class_judged_in_the_table_exists() -> None:
    missing: list[str] = []
    for group in groups():
        if group.module in CHANNEL_MODULES or group.module == "psutil":
            continue
        head, *path = group.module.split(".")
        owner: object = builtins if head == "builtins" else _MODULES[head]
        for part in path:
            owner = getattr(owner, part)
        missing.extend(
            f"{group.module}.{name}"
            for name in group.names
            if "*" not in name and not hasattr(owner, name)
        )
    assert missing == []


def test_no_channel_module_is_judged_by_a_bare_wildcard() -> None:
    bare = [
        group.module for group in groups() if group.module in CHANNEL_MODULES and "*" in group.names
    ]
    assert bare == [], (
        "judge every name of these modules by name, or by a prefix family such as `AF_*`: "
        "a bare `*` lets a function added to the module inherit a kind nobody chose"
    )


# meiva:note
# One representative name for each kind of judgment, pinned against the table: moving a name to
# another group, or dropping it from the group that carries its kind, fails here. Every group that
# carries a kind must have a name here (the next test).
_PINNED: dict[str, AmbientKind | None] = {
    "os.environ": AmbientKind.ENVIRON,
    "os.getpid": AmbientKind.PID,
    "os.cpu_count": AmbientKind.PLATFORM,
    "os.getcwd": AmbientKind.CWD,
    "os.listdir": AmbientKind.CWD,
    "os.urandom": AmbientKind.RANDOM,
    "os.times": AmbientKind.CLOCK,
    "os.path.expanduser": AmbientKind.HOME,
    "os.path.expandvars": AmbientKind.ENVIRON,
    "os.path.abspath": AmbientKind.CWD,
    "os.path.relpath": AmbientKind.CWD,
    "sys.argv": AmbientKind.ARGV,
    "sys.executable": AmbientKind.PID,
    "sys.stdin": AmbientKind.PID,
    "sys.platform": AmbientKind.PLATFORM,
    "sys.getfilesystemencoding": AmbientKind.ENVIRON,
    "sys.version_info": None,
    "time.time": AmbientKind.CLOCK,
    "time.localtime": AmbientKind.CLOCK,
    "time.gmtime": AmbientKind.CLOCK,
    "time.asctime": AmbientKind.CLOCK,
    "time.strftime": AmbientKind.CLOCK,
    "time.monotonic": AmbientKind.CLOCK,
    "time.get_clock_info": AmbientKind.PLATFORM,
    "time.clock_getres": AmbientKind.PLATFORM,
    "time.sleep": None,
    "datetime.datetime.now": AmbientKind.CLOCK,
    "datetime.datetime.fromtimestamp": AmbientKind.CLOCK,
    "datetime.date.today": AmbientKind.CLOCK,
    "datetime.date.fromtimestamp": AmbientKind.CLOCK,
    "platform.machine": AmbientKind.PLATFORM,
    "platform.uname": AmbientKind.PLATFORM,
    "platform.python_version": None,
    "platform.uname_result": None,
    "socket.gethostname": AmbientKind.PLATFORM,
    "socket.getfqdn": AmbientKind.PLATFORM,
    "socket.if_nameindex": AmbientKind.PLATFORM,
    "socket.gethostbyname": None,
    "socket.getaddrinfo": None,
    "socket.AF_INET": None,
    "socket.SO_REUSEADDR": None,
    "multiprocessing.cpu_count": AmbientKind.PLATFORM,
    "multiprocessing.current_process": AmbientKind.PID,
    "multiprocessing.Pool": None,
    "uuid.getnode": AmbientKind.PLATFORM,
    "uuid.uuid4": AmbientKind.RANDOM,
    "uuid.uuid5": None,
    "resource.getrlimit": AmbientKind.PID,
    "resource.getpagesize": AmbientKind.PLATFORM,
    "sysconfig.get_platform": AmbientKind.PLATFORM,
    "sysconfig.get_path": AmbientKind.PID,
    "threading.current_thread": AmbientKind.PID,
    "getpass.getuser": AmbientKind.PID,
    "pwd.getpwuid": AmbientKind.PID,
    "grp.getgrgid": AmbientKind.PID,
    "random.random": AmbientKind.RANDOM,
    "random.seed": AmbientKind.RANDOM,
    "random.Random": None,
    "secrets.token_hex": AmbientKind.RANDOM,
    "secrets.compare_digest": None,
    "tempfile.gettempdir": AmbientKind.ENVIRON,
    "tempfile.mkdtemp": AmbientKind.ENVIRON,
    "tempfile.NamedTemporaryFile": AmbientKind.ENVIRON,
    "tempfile.SpooledTemporaryFile": AmbientKind.ENVIRON,
    "tempfile.gettempprefix": None,
    "shutil.which": AmbientKind.ENVIRON,
    "shutil.get_terminal_size": AmbientKind.PID,
    "locale.getlocale": AmbientKind.ENVIRON,
    "zoneinfo.TZPATH": AmbientKind.ENVIRON,
    "pathlib.Path.home": AmbientKind.HOME,
    "pathlib.Path.cwd": AmbientKind.CWD,
    "psutil.Process": AmbientKind.PID,
    "builtins.input": AmbientKind.PID,
    "fileinput.input": AmbientKind.ARGV,
    "fileinput.lineno": None,
    "site.getusersitepackages": AmbientKind.HOME,
    "site.ENABLE_USER_SITE": AmbientKind.ENVIRON,
    "site.getsitepackages": AmbientKind.PID,
    "site.main": None,
    "email.utils.formatdate": AmbientKind.CLOCK,
    "email.utils.localtime": AmbientKind.CLOCK,
    "email.utils.make_msgid": AmbientKind.RANDOM,
    "email.utils.parseaddr": None,
}


@pytest.mark.parametrize(("dotted", "kind"), sorted(_PINNED.items(), key=lambda item: item[0]))
def test_a_pinned_name_is_judged_the_kind_that_was_chosen_for_it(
    dotted: str, kind: AmbientKind | None
) -> None:
    reading = judged(dotted)
    assert reading is not None and reading.name == dotted
    assert reading.judgment.kind is kind


def test_every_group_that_carries_a_kind_has_a_pinned_name() -> None:
    unpinned = [
        f"{group.module}: {' '.join(group.names)}"
        for group in groups()
        if group.judgment.kind is not None
        and not any(
            fnmatch.fnmatchcase(pinned, f"{group.module}.{name}")
            for name in group.names
            for pinned in _PINNED
        )
    ]
    assert unpinned == [], "pin one name of each of these groups in `_PINNED`"


@pytest.mark.parametrize(
    ("dotted", "name", "kind"),
    [
        ("os.environ.get", "os.environ", AmbientKind.ENVIRON),
        ("sys.argv", "sys.argv", AmbientKind.ARGV),
        ("sys.executable", "sys.executable", AmbientKind.PID),
        ("shutil.which", "shutil.which", AmbientKind.ENVIRON),
        ("pathlib.Path.home", "pathlib.Path.home", AmbientKind.HOME),
        ("datetime.datetime.now", "datetime.datetime.now", AmbientKind.CLOCK),
        ("os.path.abspath", "os.path.abspath", AmbientKind.CWD),
        ("platform.machine", "platform.machine", AmbientKind.PLATFORM),
        ("random.random", "random.random", AmbientKind.RANDOM),
        ("getpass.getuser", "getpass.getuser", AmbientKind.PID),
    ],
)
def test_the_longest_judged_prefix_decides(dotted: str, name: str, kind: AmbientKind) -> None:
    reading = judged(dotted)
    assert reading is not None
    assert (reading.name, reading.judgment.kind) == (name, kind)


@pytest.mark.parametrize(
    "dotted",
    [
        "os.path.join",
        "os.sep",
        "sys.stdout.write",
        "datetime.datetime.strptime",
        "pathlib.Path.resolve",
    ],
)
def test_a_name_that_reads_nothing_is_judged_not_ambient(dotted: str) -> None:
    reading = judged(dotted)
    assert reading is not None
    assert reading.judgment.kind is None


@pytest.mark.parametrize(
    ("dotted", "expected"),
    [
        ("os.not_a_function", True),
        ("sys.brand_new", True),
        ("time.sleep", False),
        ("os.path", False),
        ("os.path.join", False),
        ("os.path.brand_new", True),
        ("sys._getframe", False),
        ("sys._xoptions", False),
        ("json.dumps", False),
        ("myapp.os.thing", False),
    ],
)
def test_only_an_unjudged_member_of_a_channel_module_is_unjudged(
    dotted: str, expected: bool
) -> None:
    assert unjudged(dotted) is expected


def test_the_interval_clocks_are_the_judged_names_that_say_so() -> None:
    assert {"time.monotonic", "time.perf_counter_ns", "time.process_time"} <= INTERVAL_CLOCKS
    assert "time.time" not in INTERVAL_CLOCKS


def test_a_method_read_names_a_kind_and_a_target() -> None:
    assert {method.kind for method in METHOD_READS.values()} == {
        AmbientKind.HOME,
        AmbientKind.CWD,
        AmbientKind.CLOCK,
        AmbientKind.ARGV,
    }
