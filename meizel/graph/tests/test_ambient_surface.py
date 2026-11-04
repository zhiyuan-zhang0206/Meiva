from __future__ import annotations

import ast
import builtins
import ctypes.util
import datetime
import email.utils
import fileinput
import fnmatch
import getpass
import grp
import json
import locale
import multiprocessing
import netrc
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
import ssl
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import uuid
import webbrowser
import zoneinfo
from collections import Counter
from functools import cache
from pathlib import Path

import basedpyright
import pytest

from meizel.graph.ambient_judgments import Judgment, g, later
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
    "ctypes.util": ctypes.util,
    "datetime": datetime,
    "email.utils": email.utils,
    "fileinput": fileinput,
    "getpass": getpass,
    "grp": grp,
    "locale": locale,
    "multiprocessing": multiprocessing,
    "netrc": netrc,
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
    "ssl": ssl,
    "sys": sys,
    "sysconfig": sysconfig,
    "tempfile": tempfile,
    "threading": threading,
    "time": time,
    "uuid": uuid,
    "webbrowser": webbrowser,
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
# What each channel module defines in a fresh interpreter that runs `channel_module_names.py`, a
# script that imports the standard library only: no test file, no site-package import. The test
# process is not asked: a library imported by any other test in the same process can add names to a
# stdlib module (`oss2` sets `sys.maxint`), and the table must not be judged against that. `-I`
# drops the working directory and the environment, and the script needs nothing installed, so the
# child runs the same on any install of the package.
_CHILD = Path(__file__).resolve().with_name("channel_module_names.py")


@cache
def _live_names() -> dict[str, frozenset[str]]:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(_CHILD)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {module: frozenset(names) for module, names in json.loads(done.stdout).items()}


# meiva:note
# The public names of a channel module: a clean interpreter's and the stub's, without the submodules
# (`os.path` is judged as a module of its own) and the fallbacks a platform leaves as `None`.
def _public_names(module: str) -> set[str]:
    names = set(_live_names()[module]) | _stub_names(module)
    return {
        name
        for name in names
        if not name.startswith("_") and f"{module}.{name}" not in CHANNEL_MODULES
    }


def test_every_channel_module_is_spelled_in_this_file_and_in_the_child() -> None:
    assert set(_MODULES) == CHANNEL_MODULES
    assert set(_live_names()) == CHANNEL_MODULES


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
# Every name the table judges a read, with the kinds that were chosen for it, written out here by
# hand and not derived from the table: moving a name to another group, dropping it from the group
# that carries its kind, or adding a read the table's author did not mean fails here. A name that
# reads several channels is listed under each of its kinds. The completeness test alone would stay
# green when a name only changed groups.
_READS: dict[AmbientKind, list[str]] = {
    AmbientKind.ARGV: (
        [
            "fileinput.FileInput",
            "fileinput.input",
            "locale.getpreferredencoding",
            "site.ENABLE_USER_SITE",
            "site.check_enableusersite",
            "sys._xoptions",
            "sys.argv",
            "sys.flags",
            "sys.orig_argv",
            "sys.warnoptions",
            "uuid.main",
            "webbrowser.main",
        ]
    ),
    AmbientKind.ENVIRON: (
        [
            "ctypes.util.find_library",
            "ctypes.util.test",
            "getpass.getuser",
            "locale.getdefaultlocale",
            "locale.getencoding",
            "locale.getlocale",
            "locale.getpreferredencoding",
            "locale.localeconv",
            "locale.nl_langinfo",
            "locale.resetlocale",
            "locale.setlocale",
            "os.environ",
            "os.environb",
            "os.get_exec_path",
            "os.getenv",
            "os.getenvb",
            "os.path.expandvars",
            "os.putenv",
            "os.reload_environ",
            "os.unsetenv",
            "shutil.get_terminal_size",
            "shutil.which",
            "site.ENABLE_USER_SITE",
            "site.USER_BASE",
            "site.USER_SITE",
            "site.check_enableusersite",
            "site.gethistoryfile",
            "site.getuserbase",
            "site.getusersitepackages",
            "ssl.create_default_context",
            "ssl.get_default_verify_paths",
            "sys.getfilesystemencodeerrors",
            "sys.getfilesystemencoding",
            "sysconfig.get_config_var",
            "sysconfig.get_config_vars",
            "sysconfig.get_path",
            "sysconfig.get_paths",
            "sysconfig.get_platform",
            "tempfile.NamedTemporaryFile",
            "tempfile.SpooledTemporaryFile",
            "tempfile.TemporaryDirectory",
            "tempfile.TemporaryFile",
            "tempfile.gettempdir",
            "tempfile.gettempdirb",
            "tempfile.mkdtemp",
            "tempfile.mkstemp",
            "tempfile.mktemp",
            "webbrowser.get",
            "webbrowser.main",
            "webbrowser.open",
            "webbrowser.open_new",
            "webbrowser.open_new_tab",
            "webbrowser.register",
            "webbrowser.register_X_browsers",
            "webbrowser.register_standard_browsers",
            "zoneinfo.TZPATH",
        ]
    ),
    AmbientKind.CLOCK: (
        [
            "datetime.date.fromtimestamp",
            "datetime.date.today",
            "datetime.datetime.fromtimestamp",
            "datetime.datetime.now",
            "datetime.datetime.today",
            "datetime.datetime.utcnow",
            "email.utils.formatdate",
            "email.utils.localtime",
            "email.utils.make_msgid",
            "email.utils.mktime_tz",
            "time.altzone",
            "time.asctime",
            "time.clock_gettime",
            "time.clock_gettime_ns",
            "time.ctime",
            "time.daylight",
            "time.gmtime",
            "time.localtime",
            "time.mktime",
            "time.strftime",
            "time.time",
            "time.time_ns",
            "time.timezone",
            "time.tzname",
            "uuid.main",
            "uuid.uuid1",
            "uuid.uuid6",
            "uuid.uuid7",
        ]
    ),
    AmbientKind.MONOTONIC: (
        [
            "os.times",
            "time.monotonic",
            "time.monotonic_ns",
            "time.perf_counter",
            "time.perf_counter_ns",
            "time.process_time",
            "time.process_time_ns",
            "time.thread_time",
            "time.thread_time_ns",
        ]
    ),
    AmbientKind.HOME: (
        [
            "netrc.netrc",
            "os.path.expanduser",
            "pathlib.Path.expanduser",
            "pathlib.Path.home",
            "pathlib.PosixPath.home",
            "pathlib.WindowsPath.home",
            "site.USER_BASE",
            "site.USER_SITE",
            "site.gethistoryfile",
            "site.getuserbase",
            "site.getusersitepackages",
            "sysconfig.get_config_var",
            "sysconfig.get_config_vars",
            "sysconfig.get_path",
            "sysconfig.get_paths",
        ]
    ),
    AmbientKind.CWD: (
        [
            "os.getcwd",
            "os.getcwdb",
            "os.listdir",
            "os.path.abspath",
            "os.path.relpath",
            "os.scandir",
            "pathlib.Path.absolute",
            "pathlib.Path.cwd",
            "pathlib.PosixPath.cwd",
            "pathlib.WindowsPath.cwd",
        ]
    ),
    AmbientKind.PLATFORM: (
        [
            "email.utils.make_msgid",
            "multiprocessing.cpu_count",
            "os.confstr",
            "os.cpu_count",
            "os.getloadavg",
            "os.name",
            "os.process_cpu_count",
            "os.sched_getaffinity",
            "os.supports_bytes_environ",
            "os.supports_dir_fd",
            "os.supports_effective_ids",
            "os.supports_fd",
            "os.supports_follow_symlinks",
            "os.sysconf",
            "os.uname",
            "platform.android_ver",
            "platform.architecture",
            "platform.freedesktop_os_release",
            "platform.ios_ver",
            "platform.java_ver",
            "platform.libc_ver",
            "platform.mac_ver",
            "platform.machine",
            "platform.node",
            "platform.platform",
            "platform.processor",
            "platform.release",
            "platform.system",
            "platform.system_alias",
            "platform.uname",
            "platform.version",
            "platform.win32_edition",
            "platform.win32_is_iot",
            "platform.win32_ver",
            "resource.getpagesize",
            "socket.getfqdn",
            "socket.gethostname",
            "socket.if_indextoname",
            "socket.if_nameindex",
            "socket.if_nametoindex",
            "sys.dllhandle",
            "sys.getandroidapilevel",
            "sys.getwindowsversion",
            "sys.platform",
            "sys.winver",
            "sysconfig.get_platform",
            "time.clock_getres",
            "time.get_clock_info",
            "time.pthread_getcpuclockid",
            "uuid.getnode",
            "uuid.main",
            "uuid.uuid1",
            "uuid.uuid6",
        ]
    ),
    AmbientKind.PID: (
        [
            "builtins.input",
            "ctypes.util.dllist",
            "email.utils.make_msgid",
            "fileinput.FileInput",
            "fileinput.input",
            "getpass.fallback_getpass",
            "getpass.getpass",
            "getpass.getuser",
            "getpass.unix_getpass",
            "getpass.win_getpass",
            "grp.getgrall",
            "grp.getgrgid",
            "grp.getgrnam",
            "multiprocessing.active_children",
            "multiprocessing.current_process",
            "multiprocessing.parent_process",
            "netrc.netrc",
            "os.ctermid",
            "os.device_encoding",
            "os.get_terminal_size",
            "os.getegid",
            "os.geteuid",
            "os.getgid",
            "os.getgrouplist",
            "os.getgroups",
            "os.getlogin",
            "os.getpgrp",
            "os.getpid",
            "os.getppid",
            "os.getpriority",
            "os.getresgid",
            "os.getresuid",
            "os.getuid",
            "os.isatty",
            "os.sched_getparam",
            "os.sched_getscheduler",
            "os.sched_rr_get_interval",
            "os.ttyname",
            "os.umask",
            "psutil.Process",
            "pwd.getpwall",
            "pwd.getpwnam",
            "pwd.getpwuid",
            "resource.getrlimit",
            "resource.getrusage",
            "resource.prlimit",
            "shutil.get_terminal_size",
            "site.ENABLE_USER_SITE",
            "site.PREFIXES",
            "site.check_enableusersite",
            "site.getsitepackages",
            "sys.__stdin__",
            "sys.base_exec_prefix",
            "sys.base_prefix",
            "sys.exec_prefix",
            "sys.executable",
            "sys.prefix",
            "sys.stderr.isatty",
            "sys.stdin",
            "sys.stdout.isatty",
            "sysconfig.get_config_var",
            "sysconfig.get_config_vars",
            "sysconfig.get_path",
            "sysconfig.get_paths",
            "threading.activeCount",
            "threading.active_count",
            "threading.currentThread",
            "threading.current_thread",
            "threading.enumerate",
            "threading.get_ident",
            "threading.get_native_id",
            "threading.main_thread",
        ]
    ),
    AmbientKind.RANDOM: (
        [
            "email.utils.make_msgid",
            "os.getrandom",
            "os.urandom",
            "random.SystemRandom",
            "random.betavariate",
            "random.binomialvariate",
            "random.choice",
            "random.choices",
            "random.expovariate",
            "random.gammavariate",
            "random.gauss",
            "random.getrandbits",
            "random.getstate",
            "random.lognormvariate",
            "random.normalvariate",
            "random.paretovariate",
            "random.randbytes",
            "random.randint",
            "random.random",
            "random.randrange",
            "random.sample",
            "random.seed",
            "random.setstate",
            "random.shuffle",
            "random.triangular",
            "random.uniform",
            "random.vonmisesvariate",
            "random.weibullvariate",
            "secrets.SystemRandom",
            "secrets.choice",
            "secrets.randbelow",
            "secrets.randbits",
            "secrets.token_bytes",
            "secrets.token_hex",
            "secrets.token_urlsafe",
            "ssl.RAND_bytes",
            "ssl.RAND_pseudo_bytes",
            "ssl.RAND_status",
            "tempfile.NamedTemporaryFile",
            "tempfile.SpooledTemporaryFile",
            "tempfile.TemporaryDirectory",
            "tempfile.TemporaryFile",
            "tempfile.mkdtemp",
            "tempfile.mkstemp",
            "tempfile.mktemp",
            "uuid.getnode",
            "uuid.main",
            "uuid.uuid1",
            "uuid.uuid4",
            "uuid.uuid6",
            "uuid.uuid7",
            "uuid.uuid8",
        ]
    ),
}


# meiva:note
# A representative of each way a name reads nothing, so that a name does not drift into a kind
# from the other side.
_NOT_READS: list[str] = [
    "sys.version_info",
    "time.sleep",
    "platform.python_version",
    "platform.uname_result",
    "socket.gethostbyname",
    "socket.getaddrinfo",
    "socket.AF_INET",
    "socket.SO_REUSEADDR",
    "multiprocessing.Pool",
    "uuid.uuid5",
    "random.Random",
    "secrets.compare_digest",
    "tempfile.gettempprefix",
    "fileinput.lineno",
    "site.main",
    "email.utils.parseaddr",
]


@pytest.mark.parametrize(
    ("dotted", "kind"),
    sorted((name, kind) for kind, names in _READS.items() for name in names),
)
def test_a_name_is_judged_the_kind_that_was_chosen_for_it(dotted: str, kind: AmbientKind) -> None:
    reading = judged(dotted)
    assert reading is not None and reading.name == dotted
    assert kind in reading.judgment.kinds


def _pinned_kinds() -> dict[str, frozenset[AmbientKind]]:
    pinned: dict[str, set[AmbientKind]] = {}
    for kind, names in _READS.items():
        for name in names:
            pinned.setdefault(name, set()).add(kind)
    return {name: frozenset(kinds) for name, kinds in pinned.items()}


def test_the_pinned_reads_are_exactly_the_reads_of_the_table() -> None:
    judged_as_reads = {
        f"{group.module}.{name}": frozenset(group.judgment.kinds)
        for group in groups()
        if group.judgment.kinds
        for name in group.names
    }
    pinned = _pinned_kinds()
    assert sorted(judged_as_reads.keys() - pinned.keys()) == [], "pin these names in `_READS`"
    assert sorted(pinned.keys() - judged_as_reads.keys()) == [], "these names are no longer reads"
    assert {name for name in pinned if pinned[name] != judged_as_reads[name]} == set(), (
        "these names read other kinds than the ones pinned"
    )
    assert sum(len(names) for names in _READS.values()) == sum(
        len(kinds) for kinds in pinned.values()
    ), "a name is pinned twice under one kind"


# meiva:note
# The calls that read several channels at once, with every channel written out: a caller that pays
# one of them back still owes the others, and a table that records one kind per name loses them.
_MULTIPLE: dict[str, frozenset[AmbientKind]] = {
    "email.utils.make_msgid": frozenset(
        {AmbientKind.CLOCK, AmbientKind.PID, AmbientKind.RANDOM, AmbientKind.PLATFORM}
    ),
    "tempfile.mkdtemp": frozenset({AmbientKind.ENVIRON, AmbientKind.RANDOM}),
    "tempfile.mkstemp": frozenset({AmbientKind.ENVIRON, AmbientKind.RANDOM}),
    "tempfile.NamedTemporaryFile": frozenset({AmbientKind.ENVIRON, AmbientKind.RANDOM}),
    "getpass.getuser": frozenset({AmbientKind.ENVIRON, AmbientKind.PID}),
    "uuid.uuid1": frozenset({AmbientKind.CLOCK, AmbientKind.PLATFORM, AmbientKind.RANDOM}),
    "shutil.get_terminal_size": frozenset({AmbientKind.ENVIRON, AmbientKind.PID}),
    "site.getuserbase": frozenset({AmbientKind.ENVIRON, AmbientKind.HOME}),
    "sysconfig.get_path": frozenset({AmbientKind.PID, AmbientKind.ENVIRON, AmbientKind.HOME}),
    "sysconfig.get_config_vars": frozenset(
        {AmbientKind.PID, AmbientKind.ENVIRON, AmbientKind.HOME}
    ),
    "fileinput.input": frozenset({AmbientKind.ARGV, AmbientKind.PID}),
    "netrc.netrc": frozenset({AmbientKind.HOME, AmbientKind.PID}),
}


@pytest.mark.parametrize(("dotted", "kinds"), sorted(_MULTIPLE.items()))
def test_a_call_that_reads_several_channels_is_judged_every_one_of_them(
    dotted: str, kinds: frozenset[AmbientKind]
) -> None:
    reading = judged(dotted)
    assert reading is not None and reading.name == dotted
    assert frozenset(reading.judgment.kinds) == kinds
    assert len(reading.judgment.kinds) == len(kinds), "a kind is listed twice"


def test_a_judgment_that_lists_a_kind_twice_or_reads_nothing_under_a_clause_is_refused() -> None:
    with pytest.raises(ValueError, match="listed twice"):
        Judgment((AmbientKind.CLOCK, AmbientKind.CLOCK), "twice")
    with pytest.raises(ValueError, match="spelled as the kind"):
        g("time", "time", (AmbientKind.CLOCK,), "a tuple of one kind")
    with pytest.raises(ValueError, match="reads nothing"):
        g("time", "time", None, "a clause on a name that reads nothing", unless=later(0, "x"))
    with pytest.raises(ValueError, match="nothing else"):
        g("time", "time", (AmbientKind.CLOCK, AmbientKind.PID), "two kinds", interval=True)
    with pytest.raises(ValueError, match="nothing else"):
        g("time", "time", AmbientKind.CLOCK, "the wall clock is not a duration", interval=True)


@pytest.mark.parametrize("dotted", _NOT_READS)
def test_a_representative_name_that_reads_nothing_is_judged_so(dotted: str) -> None:
    reading = judged(dotted)
    assert reading is not None and reading.name == dotted
    assert reading.judgment.kinds == ()


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
        ("getpass.getpass", "getpass.getpass", AmbientKind.PID),
    ],
)
def test_the_longest_judged_prefix_decides(dotted: str, name: str, kind: AmbientKind) -> None:
    reading = judged(dotted)
    assert reading is not None
    assert (reading.name, reading.judgment.kinds) == (name, (kind,))


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
    assert reading.judgment.kinds == ()


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


def test_an_interval_clock_is_a_duration_and_the_wall_clock_is_an_instant() -> None:
    judged_monotonic = judged("time.monotonic")
    judged_wall = judged("time.time")
    assert judged_monotonic is not None and judged_wall is not None
    assert judged_monotonic.judgment.kinds == (AmbientKind.MONOTONIC,)
    assert judged_wall.judgment.kinds == (AmbientKind.CLOCK,)
    judged_times = judged("os.times")
    assert judged_times is not None
    assert judged_times.judgment.kinds == (AmbientKind.MONOTONIC,)


def test_a_method_read_names_a_kind_and_a_target() -> None:
    assert {method.kind for method in METHOD_READS.values()} == {
        AmbientKind.HOME,
        AmbientKind.CWD,
        AmbientKind.CLOCK,
        AmbientKind.ARGV,
    }
