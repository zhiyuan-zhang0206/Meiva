# meiva:note
# The standard library's windows onto the ambient world, judged name by name.
#
# A process learns something it was not told through a small number of operating-system channels:
# its arguments, its environment, its own identity, the host's identity, the clock and the time zone,
# entropy, the anchors a relative path hangs from, the contents of files, the network, and its
# standard streams. This module walks those channels module by module and judges every public name
# of the modules that carry them: which kind of ambient read it is, or why it is not one. A name that
# is not judged cannot be told from a read the vocabulary forgot, so a use of an unjudged name in a
# channel module is a diagnostic (`ambient-unjudged`) and the table is checked against the modules
# themselves (`meizel/graph/tests/test_ambient_surface.py`).
#
# The channels that are not ambient kinds are judged by the other layers: the contents of files and
# the network are effects (`impure-call`), and a name that only converts or compares values reads
# nothing. A call that reads the world only when it is not handed a value carries an `unless` clause,
# the one mechanism a policy reader uses.

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from functools import cache

from meizel.graph.ambient_judgments import (
    BUILD,
    CONSTANT,
    FILES,
    NET,
    TYPE,
    Group,
    Judgment,
    K,
    g,
    later,
)
from meizel.graph.ambient_process_table import PROCESS_GROUPS
from meizel.graph.model import AmbientKind, Unless

__all__ = [
    "CHANNEL_MODULES",
    "INTERVAL_CLOCKS",
    "LOOP_CLOCK",
    "METHOD_READS",
    "SEEDABLE_GENERATORS",
    "MethodReading",
    "Reading",
    "groups",
    "judged",
    "unjudged",
]


# meiva:note
# The judged name that a dotted name resolved to, and its judgment.
@dataclass(frozen=True, slots=True)
class Reading:
    name: str
    judgment: Judgment


# meiva:note
# A method whose receiver the analysis cannot resolve, judged by its name and what it is given.
@dataclass(frozen=True, slots=True)
class MethodReading:
    kind: AmbientKind
    target: str
    unless: Unless | None
    caller_exempt: bool = False


# meiva:note
# The channel modules whose every public name is judged: a name of one of them that no entry judges
# is `ambient-unjudged`. A module that is not here may still carry an entry (`psutil.Process`).
CHANNEL_MODULES = frozenset(
    {
        "datetime",
        "getpass",
        "grp",
        "locale",
        "multiprocessing",
        "os",
        "os.path",
        "pathlib",
        "platform",
        "pwd",
        "random",
        "resource",
        "secrets",
        "shutil",
        "socket",
        "sys",
        "sysconfig",
        "tempfile",
        "threading",
        "time",
        "uuid",
        "zoneinfo",
    }
)

# meiva:note
# The host: `platform` and `socket.gethostname` are identity; everything else of `socket` is network.
_HOST = (
    g(
        "platform",
        "*",
        K.PLATFORM,
        "every `platform` function asks the machine or the interpreter",
    ),
    g(
        "socket",
        "gethostname getfqdn if_nameindex if_nametoindex if_indextoname",
        K.PLATFORM,
        "the host's name or its network interfaces",
    ),
    g("socket", "*", None, NET),
    g("multiprocessing", "cpu_count", K.PLATFORM, "the host's processor count"),
    g(
        "multiprocessing",
        "current_process parent_process active_children",
        K.PID,
        "what the process says about itself",
    ),
    g(
        "multiprocessing",
        "*",
        None,
        "a process, pool, queue, lock or start-method setting: an effect or a constructor",
    ),
    g("uuid", "getnode", K.PLATFORM, "the host's hardware address"),
    g(
        "uuid",
        "uuid1 uuid4 uuid6 uuid7 uuid8",
        K.RANDOM,
        "a version of UUID that draws from the clock, the host or entropy",
    ),
    g(
        "uuid",
        "uuid3 uuid5 UUID SafeUUID NAMESPACE_* RESERVED_* RFC_4122 NIL MAX Enum bytes_ int_ main",
        None,
        "a name-derived UUID, a type or a constant: it reads nothing but its input",
    ),
    g(
        "resource",
        "getrlimit prlimit getrusage",
        K.PID,
        "the process's own limits and resource usage",
    ),
    g("resource", "getpagesize", K.PLATFORM, "the host's page size"),
    g(
        "resource",
        "setrlimit error struct_rusage RLIM* RLIMIT_* RUSAGE_*",
        None,
        "changes a limit or is a type or constant",
    ),
    g("sysconfig", "get_platform", K.PLATFORM, "the platform the interpreter was built for"),
    g(
        "sysconfig",
        "get_path get_paths",
        K.PID,
        "the interpreter's install paths, derived from its prefix and the user base",
    ),
    g(
        "sysconfig",
        "get_config_var get_config_vars get_path_names get_scheme_names "
        "get_default_scheme get_preferred_scheme get_python_version get_makefile_filename "
        "get_config_h_filename is_python_build parse_config_h expand_makefile_vars realpath",
        None,
        BUILD,
    ),
    g(
        "threading",
        "get_ident get_native_id current_thread currentThread main_thread active_count "
        "activeCount enumerate",
        K.PID,
        "what the process says about its own threads",
    ),
    g(
        "threading",
        "Thread Timer Lock RLock Condition Semaphore BoundedSemaphore Event Barrier "
        "BrokenBarrierError ThreadError local WeakSet ExceptHookArgs TIMEOUT_MAX excepthook "
        "setprofile settrace getprofile gettrace setprofile_all_threads settrace_all_threads "
        "stack_size concurrent_tee serialize_iterator synchronized_iterator",
        None,
        "a thread, a synchronisation primitive or a hook into the interpreter: an effect or a type",
    ),
)

# meiva:note
# Identity and entropy: the user the process runs as, and every draw of randomness.
_IDENTITY = (
    g(
        "getpass",
        "getuser getpass fallback_getpass unix_getpass win_getpass",
        K.PID,
        "who the process runs as, or what the controlling terminal answers",
    ),
    g("getpass", "GetPassWarning", None, TYPE),
    g(
        "pwd",
        "getpwuid getpwnam getpwall",
        K.PID,
        "the account database: the entry of the process's own user is the common use",
    ),
    g("pwd", "struct_passwd", None, TYPE),
    g(
        "grp",
        "getgrgid getgrnam getgrall",
        K.PID,
        "the group database: the process's own groups are the common use",
    ),
    g("grp", "struct_group", None, TYPE),
    g("random", "*", K.RANDOM, "every draw, seed and state of the shared generator"),
    g(
        "random",
        "Random",
        None,
        "the class alone reads nothing; a constructor call with no literal seed is judged at the call",
    ),
    g("random", "BPF LOG4 NV_MAGICCONST RECIP_BPF SG_MAGICCONST TWOPI", None, CONSTANT),
    g("secrets", "*", K.RANDOM, "every `secrets` function draws from operating-system entropy"),
    g(
        "secrets",
        "compare_digest DEFAULT_ENTROPY",
        None,
        "a comparison or a constant: it draws nothing",
    ),
)

# meiva:note
# The anchors of a path, the temporary directory, the locale and the program search path.
_ANCHORS = (
    g(
        "tempfile",
        "gettempdir gettempdirb",
        K.ENVIRON,
        "the temporary directory, taken from TMPDIR",
    ),
    g(
        "tempfile",
        "mkdtemp mkstemp mktemp TemporaryDirectory",
        K.ENVIRON,
        "creates it in the temporary directory taken from TMPDIR unless it is given a directory",
        unless=later(2, "dir"),
    ),
    g(
        "tempfile",
        "NamedTemporaryFile TemporaryFile",
        K.ENVIRON,
        "creates it in the temporary directory taken from TMPDIR unless it is given a directory",
        unless=later(6, "dir"),
    ),
    g(
        "tempfile",
        "SpooledTemporaryFile",
        K.ENVIRON,
        "rolls over into the temporary directory taken from TMPDIR unless it is given a directory",
        unless=later(7, "dir"),
    ),
    g(
        "tempfile",
        "gettempprefix gettempprefixb TMP_MAX template tempdir",
        None,
        "the constant prefix of a name; `tempdir` is an override that is assigned, not read",
    ),
    g(
        "shutil",
        "which",
        K.ENVIRON,
        "searches PATH unless it is given a path",
        unless=later(2, "path"),
    ),
    g(
        "shutil",
        "get_terminal_size",
        K.PID,
        "the size of the terminal the process is attached to, from the environment and the device",
    ),
    g(
        "shutil",
        "copy copy2 copyfile copyfileobj copymode copystat copytree move rmtree chown "
        "make_archive unpack_archive disk_usage",
        None,
        FILES,
    ),
    g(
        "shutil",
        "get_archive_formats get_unpack_formats register_archive_format "
        "register_unpack_format unregister_archive_format unregister_unpack_format ignore_patterns "
        "Error ExecError ReadError RegistryError SameFileError SpecialFileError COPY_BUFSIZE",
        None,
        "a registry of archive formats, a filter or an error type: it reads nothing",
    ),
    g(
        "locale",
        "getlocale getdefaultlocale getpreferredencoding getencoding setlocale "
        "resetlocale nl_langinfo localeconv",
        K.ENVIRON,
        "the locale, taken from the environment variables LC_*, LANG and the operating system",
    ),
    g(
        "locale",
        "atof atoi currency delocalize format format_string localize str strcoll strxfrm",
        None,
        "formats with the process-wide locale, which `setlocale` or `getlocale` reads",
    ),
    g(
        "locale",
        "ABDAY_* ABMON_* DAY_* MON_* LC_* ALT_DIGITS AM_STR PM_STR CHAR_MAX CODESET "
        "CRNCYSTR D_FMT D_T_FMT T_FMT T_FMT_AMPM ERA ERA_* NOEXPR YESEXPR RADIXCHAR THOUSEP",
        None,
        CONSTANT,
    ),
    g(
        "locale",
        "Error normalize locale_alias locale_encoding_alias windows_locale "
        "bind_textdomain_codeset bindtextdomain dcgettext dgettext gettext textdomain",
        None,
        "an error type, a table of locale names or the message-catalogue binding: it reads nothing",
    ),
    g(
        "zoneinfo",
        "TZPATH",
        K.ENVIRON,
        "the search path of the time-zone database, from PYTHONTZPATH",
    ),
    g(
        "zoneinfo",
        "ZoneInfo ZoneInfoNotFoundError InvalidTZPathWarning available_timezones reset_tzpath",
        None,
        "a zone is named by the caller and read from the database, a file read the purity layer judges",
    ),
    g(
        "pathlib",
        "Path.home PosixPath.home WindowsPath.home Path.expanduser",
        K.HOME,
        "the user's home directory",
    ),
    g(
        "pathlib",
        "Path.cwd PosixPath.cwd WindowsPath.cwd Path.absolute",
        K.CWD,
        "the working directory",
    ),
    g(
        "pathlib",
        "Path PosixPath WindowsPath PurePath PurePosixPath PureWindowsPath",
        None,
        "path arithmetic over the values it is given; reading the file system is the purity layer's "
        "(`impure-call`), and `Path.resolve` canonicalises a path that is already held",
    ),
    g(
        "pathlib",
        "UnsupportedOperation EBADF ELOOP ENOENT ENOTDIR S_IS* Sequence urlquote_from_bytes",
        None,
        "an error type, a constant or a name imported for the module's own use",
    ),
)

# meiva:note
# The non-standard-library readers that are judged because the repository uses them as the standard
# library's: they carry no closure.
_THIRD_PARTY = (
    g("psutil", "Process", K.PID, "this process when it is given no pid", unless=later(0, "pid")),
)


def groups() -> tuple[Group, ...]:
    return (
        *PROCESS_GROUPS,
        *_HOST,
        *_IDENTITY,
        *_ANCHORS,
        *_THIRD_PARTY,
    )


# meiva:note
# The names of `groups()` as `dotted name -> judgment`, and the `*` patterns by module.
@cache
def _index() -> tuple[dict[str, Judgment], dict[str, tuple[tuple[str, Judgment], ...]]]:
    exact: dict[str, Judgment] = {}
    patterns: dict[str, list[tuple[str, Judgment]]] = {}
    for group in groups():
        for name in group.names:
            if "*" in name:
                patterns.setdefault(group.module, []).append((name, group.judgment))
            else:
                exact[f"{group.module}.{name}"] = group.judgment
    return exact, {module: tuple(found) for module, found in patterns.items()}


# meiva:note
# The judgment of the longest judged prefix of a dotted name: `os.environ.get` is judged as
# `os.environ`, `datetime.datetime.now` as itself. A prefix is judged by an entry of its own, or by
# a pattern of the module (or class) that holds it.
def judged(dotted: str) -> Reading | None:
    parts = dotted.split(".")
    if parts[0] not in _TOP_LEVEL:
        return None
    exact, patterns = _index()
    for end in range(len(parts), 1, -1):
        name = ".".join(parts[:end])
        found = exact.get(name)
        if found is not None:
            return Reading(name, found)
        module, member = ".".join(parts[: end - 1]), parts[end - 1]
        for pattern, judgment in patterns.get(module, ()):
            if fnmatch.fnmatchcase(member, pattern):
                return Reading(name, judgment)
    return None


# meiva:note
# Whether a dotted name is a member of a channel module that no entry judges. The module itself
# (`os.path`, reached by `import os.path`) is a name of the module system, not a member, and a
# private name (`sys._getframe`) is not part of the interface the table covers unless an entry
# lists it (`sys._xoptions`).
def unjudged(dotted: str) -> bool:
    parts = dotted.split(".")
    if parts[0] not in _TOP_LEVEL or judged(dotted) is not None:
        return False
    if any(part.startswith("_") for part in parts[1:]):
        return False
    for end in range(len(parts), 0, -1):
        module = ".".join(parts[:end])
        if module in CHANNEL_MODULES:
            return end < len(parts)
    return False


_TOP_LEVEL = frozenset(module.split(".")[0] for module in CHANNEL_MODULES) | {
    group.module.split(".")[0] for group in groups()
}


# meiva:note
# The interval clocks: reading one for a deadline, a duration or a TTL is the plain use of `time`
# that the conventions allow in code no test needs to steer; the read is a decision worth a root
# only where the same function also waits, because then how long to wait next is decided in a loop.
# The wall clock is always a window onto the world.
INTERVAL_CLOCKS = frozenset(name for name, judgment in _index()[0].items() if judgment.interval)

# meiva:note
# The target recorded for `loop.time()` of the running event loop, the interval clock of asyncio.
LOOP_CLOCK = "asyncio.loop.time"

# meiva:note
# Constructors whose seed must be inspected at the call, rather than at a class reference.
SEEDABLE_GENERATORS = frozenset({"random.Random"})

# meiva:note
# Methods whose receiver the analysis cannot resolve (it has no types), judged by their name and by
# what a call gives them. The name must be one that only the readers listed here carry in
# practice: an argument parser's `parse_args`, a datetime's `astimezone`, a path's `absolute` and
# `expanduser`. A path built from the caller's own parameters is the caller's value, not a read.
METHOD_READS: dict[str, MethodReading] = {
    "expanduser": MethodReading(K.HOME, "pathlib.Path.expanduser", None, caller_exempt=True),
    "absolute": MethodReading(K.CWD, "pathlib.Path.absolute", None, caller_exempt=True),
    "astimezone": MethodReading(K.CLOCK, "datetime.datetime.astimezone", later(0, "tz")),
    "parse_args": MethodReading(K.ARGV, "argparse.ArgumentParser.parse_args", later(0, "args")),
    "parse_known_args": MethodReading(
        K.ARGV, "argparse.ArgumentParser.parse_known_args", later(0, "args")
    ),
    "parse_intermixed_args": MethodReading(
        K.ARGV, "argparse.ArgumentParser.parse_intermixed_args", later(0, "args")
    ),
    "parse_known_intermixed_args": MethodReading(
        K.ARGV, "argparse.ArgumentParser.parse_known_intermixed_args", later(0, "args")
    ),
}
