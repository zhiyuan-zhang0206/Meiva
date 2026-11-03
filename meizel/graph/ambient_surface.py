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
# the one mechanism a policy reader uses. A call that reads several channels at once lists every
# kind (`kinds`), so that a caller which pays one of the reads back still has the others on its books.

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from functools import cache

from meizel.graph.ambient_judgments import (
    BUILD,
    CONSTANT,
    FILES,
    NET,
    STATE,
    TYPE,
    VALUES,
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
        "ctypes.util",
        "datetime",
        "email.utils",
        "fileinput",
        "getpass",
        "grp",
        "locale",
        "multiprocessing",
        "netrc",
        "os",
        "os.path",
        "pathlib",
        "platform",
        "pwd",
        "random",
        "resource",
        "secrets",
        "shutil",
        "site",
        "socket",
        "ssl",
        "sys",
        "sysconfig",
        "tempfile",
        "threading",
        "time",
        "uuid",
        "webbrowser",
        "zoneinfo",
    }
)

# meiva:note
# The host: `platform` and `socket.gethostname` are identity; everything else of `socket` is network.
# No module is judged by a bare `*`: every name is listed, or belongs to a family named by a prefix
# (`AF_*`, `SO_*`), so a function added to a module can never inherit a judgment unseen.
_HOST = (
    g(
        "platform",
        "architecture freedesktop_os_release android_ver ios_ver java_ver libc_ver mac_ver machine "
        "node platform processor release system system_alias uname version win32_edition "
        "win32_is_iot win32_ver",
        K.PLATFORM,
        "asks the machine or the operating system what it is",
    ),
    g(
        "platform",
        "python_branch python_build python_compiler python_implementation python_revision "
        "python_version python_version_tuple",
        None,
        BUILD,
    ),
    g("platform", "AndroidVer IOSVersionInfo uname_result", None, TYPE),
    g(
        "platform",
        "invalidate_caches",
        None,
        "empties the module's own cache of what it asked the machine",
    ),
    g(
        "socket",
        "gethostname getfqdn if_nameindex if_nametoindex if_indextoname",
        K.PLATFORM,
        "the host's name or its network interfaces",
    ),
    g(
        "socket",
        "socket SocketType SocketIO socketpair fromfd fromshare dup close create_connection "
        "create_server getaddrinfo getdefaulttimeout setdefaulttimeout gethostbyaddr gethostbyname "
        "gethostbyname_ex getnameinfo getprotobyname getservbyname getservbyport sethostname "
        "has_dualstack_ipv6 has_ipv6 herror gaierror error timeout errorTab CAPI AddressFamily "
        "AddressInfo SocketKind MsgFlag IntEnum IntFlag EAGAIN EBADF EWOULDBLOCK SOMAXCONN htonl "
        "htons ntohl ntohs inet_aton inet_ntoa inet_ntop inet_pton recv_fds send_fds",
        None,
        NET,
    ),
    g(
        "socket",
        "AF_* AI_* ALG_* BDADDR_* BT_* BTPROTO_* CAN_* CMSG_* EAI_* ETH_* ETHERTYPE_* HCI_* HV_* "
        "HVSOCKET_* INADDR_* IOCTL_* IP_* IPPORT_* IPPROTO_* IPV6_* IPX_* J1939_* L2CAP_* LOCAL_* "
        "MSG_* NETLINK_* NI_* PACKET_* PF_* RCVALL_* RDS_* SCM_* SHUT_* SIO_* SO_* SOCK_* SOL_* "
        "SYSPROTO_* TCP_* TIPC_* UDPLITE_* VM_* VMADDR_*",
        None,
        "a constant of the network headers, one family per prefix: it selects an option of the "
        "socket API and identifies nothing about the host",
    ),
    g("multiprocessing", "cpu_count", K.PLATFORM, "the host's processor count"),
    g(
        "multiprocessing",
        "current_process parent_process active_children",
        K.PID,
        "what the process says about itself",
    ),
    g(
        "multiprocessing",
        "Array AuthenticationError Barrier BoundedSemaphore BufferTooShort Condition Event "
        "JoinableQueue Lock Manager Pipe Pool Process ProcessError Queue RLock RawArray RawValue "
        "SUBDEBUG SUBWARNING Semaphore SimpleQueue TimeoutError Value allow_connection_pickling "
        "freeze_support get_all_start_methods get_context get_logger get_start_method "
        "log_to_stderr set_executable set_forkserver_preload set_start_method",
        None,
        "a process, pool, queue, lock or start-method setting: an effect or a constructor",
    ),
    g(
        "uuid",
        "getnode",
        (K.PLATFORM, K.RANDOM),
        "the host's hardware address, which is a random 48-bit number when the host yields none",
    ),
    g(
        "uuid",
        "uuid1 uuid6",
        (K.CLOCK, K.PLATFORM, K.RANDOM),
        "the current time, the host's hardware address and a random clock sequence",
    ),
    g("uuid", "uuid7", (K.CLOCK, K.RANDOM), "the current time and random bytes"),
    g("uuid", "uuid4 uuid8", K.RANDOM, "random bytes drawn from operating-system entropy"),
    g(
        "uuid",
        "main",
        (K.ARGV, K.CLOCK, K.PLATFORM, K.RANDOM),
        "the command-line entry of the module: reads sys.argv and prints any version it is asked for",
    ),
    g(
        "uuid",
        "uuid3 uuid5 UUID SafeUUID NAMESPACE_* RESERVED_* RFC_4122 NIL MAX Enum bytes_ int_",
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
    g(
        "sysconfig",
        "get_platform",
        (K.PLATFORM, K.ENVIRON),
        "the platform the interpreter was built for, unless `_PYTHON_HOST_PLATFORM` names another "
        "(a cross-compilation override) or, on macOS, `ARCHFLAGS` names the architectures",
    ),
    g(
        "sysconfig",
        "get_path get_paths get_config_var get_config_vars",
        (K.PID, K.ENVIRON, K.HOME),
        "the interpreter's install paths and configuration variables, derived from its prefix, and "
        "from the user base that the first call computes for every variable (`PYTHONUSERBASE`, else "
        "a path under the user's home directory) and, on macOS, from CC, CFLAGS and ARCHFLAGS",
    ),
    g(
        "sysconfig",
        "get_path_names get_scheme_names "
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
        "getuser",
        (K.ENVIRON, K.PID),
        "who the process runs as: LOGNAME, USER, LNAME or USERNAME, else the account database "
        "entry of the process's own user id",
    ),
    g(
        "getpass",
        "getpass fallback_getpass unix_getpass win_getpass",
        K.PID,
        "what the controlling terminal answers",
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
    g(
        "random",
        "betavariate binomialvariate choice choices expovariate gammavariate gauss getrandbits "
        "getstate lognormvariate normalvariate paretovariate randbytes randint random randrange "
        "sample seed setstate shuffle triangular uniform vonmisesvariate weibullvariate "
        "SystemRandom",
        K.RANDOM,
        "every draw, seed and state of the shared generator, and the class that draws from the "
        "operating system",
    ),
    g(
        "random",
        "Random",
        None,
        "the class alone reads nothing; a constructor call with no literal seed is judged at the call",
    ),
    g("random", "BPF LOG4 NV_MAGICCONST RECIP_BPF SG_MAGICCONST TWOPI", None, CONSTANT),
    g(
        "secrets",
        "choice randbelow randbits token_bytes token_hex token_urlsafe SystemRandom",
        K.RANDOM,
        "every `secrets` function draws from operating-system entropy",
    ),
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
        "the temporary directory, taken from TMPDIR; the last resort of its search, the working "
        "directory, is reached only when no system directory is writable",
    ),
    g(
        "tempfile",
        "mkdtemp mkstemp mktemp TemporaryDirectory",
        (K.ENVIRON, K.RANDOM),
        "creates it under a random name, in the temporary directory taken from TMPDIR, unless it is "
        "given a directory; the `unless` clause spares the call as a whole, so a call that names "
        "its directory still draws the random name and records nothing",
        unless=later(2, "dir"),
    ),
    g(
        "tempfile",
        "NamedTemporaryFile TemporaryFile",
        (K.ENVIRON, K.RANDOM),
        "creates it under a random name (where the platform has no anonymous files), in the "
        "temporary directory taken from TMPDIR, unless it is given a directory; as for `mkdtemp`, "
        "a call that names its directory records nothing",
        unless=later(6, "dir"),
    ),
    g(
        "tempfile",
        "SpooledTemporaryFile",
        (K.ENVIRON, K.RANDOM),
        "rolls over into a file of a random name in the temporary directory taken from TMPDIR "
        "unless it is given a directory; as for `mkdtemp`, a call that names its directory "
        "records nothing",
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
        (K.ENVIRON, K.PID),
        "the size of the terminal: COLUMNS and LINES, else the device the process's standard output "
        "is attached to",
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
        "getpreferredencoding",
        (K.ENVIRON, K.ARGV),
        "the locale's encoding, taken from the environment variables LC_*, LANG and the operating "
        "system, unless the interpreter was started in UTF-8 mode (`-X utf8`), which answers first",
    ),
    g(
        "locale",
        "getlocale getdefaultlocale getencoding setlocale resetlocale nl_langinfo localeconv",
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
# What the process is fed besides its arguments and its environment: a line of standard input, the
# files its command line names, the user's site directories, and the clock a mail header stamps.
_FED = (
    g(
        "builtins",
        "input",
        K.PID,
        "names standard input as the stream it reads and writes the prompt to standard output: how "
        "the process is wired is its own statement, and the contents are an effect on a descriptor "
        "(`sys.stdin` is judged the same way)",
    ),
    g(
        "fileinput",
        "input FileInput",
        (K.ARGV, K.PID),
        "reads the files named in sys.argv[1:], and standard input when there are none, unless it "
        "is given the files",
        unless=later(0, "files"),
    ),
    g(
        "fileinput",
        "close nextfile filename lineno filelineno fileno isfirstline isstdin hook_compressed "
        "hook_encoded GenericAlias",
        None,
        "the state of an iteration the caller started, a hook for it or a type: it reads nothing "
        "the caller did not name",
    ),
    g(
        "site",
        "getuserbase getusersitepackages gethistoryfile USER_BASE USER_SITE",
        (K.ENVIRON, K.HOME),
        "the user's base directory: PYTHONUSERBASE (PYTHON_HISTORY for the history file), else a "
        "path under the home directory",
    ),
    g(
        "site",
        "ENABLE_USER_SITE check_enableusersite",
        (K.ENVIRON, K.ARGV, K.PID),
        "whether the user site is on: the interpreter's flags (`-s`, and PYTHONNOUSERSITE which "
        "sets the same flag) and whether the real and effective user and group ids agree",
    ),
    g(
        "site",
        "getsitepackages PREFIXES",
        K.PID,
        "the interpreter's own site directories, derived from its prefix",
    ),
    g(
        "site",
        "StartupState abs_paths addpackage addsitedir addsitepackages addusersitepackages "
        "enablerlcompleter execsitecustomize execusercustomize main makepath register_readline "
        "removeduppaths setcopyright sethelper setquit venv",
        None,
        STATE,
    ),
    g(
        "email.utils",
        "formatdate",
        K.CLOCK,
        "stamps the current time unless it is given the time, and with `localtime=True` renders any "
        "time in the host's time zone; the `unless` clause cannot say \"given a time and not "
        '`localtime`", so every call is recorded, which over-records the pure `formatdate(1.5)`',
    ),
    g(
        "email.utils",
        "localtime mktime_tz",
        K.CLOCK,
        "converts into, or out of, the host's time zone, as `time.localtime` and `time.mktime` do",
    ),
    g(
        "email.utils",
        "make_msgid",
        (K.CLOCK, K.PID, K.RANDOM, K.PLATFORM),
        "draws a message id from the clock, the process id, a random number and, without a "
        "`domain`, the host name; a call that gives a `domain` still reads the other three, and "
        "the clause cannot spare the host name alone, so the host name is recorded for it too",
    ),
    g(
        "email.utils",
        "COMMASPACE CRLF Charset EMPTYSTRING TICK UEMPTYSTRING collapse_rfc2231_value "
        "decode_params decode_rfc2231 encode_rfc2231 escapesre format_datetime formataddr "
        "getaddresses parseaddr parsedate parsedate_to_datetime parsedate_tz quote "
        "rfc2231_continuation specialsre supports_strict_parsing unquote",
        None,
        VALUES,
    ),
)

# meiva:note
# The credentials and certificates a process finds on its own: `~/.netrc`, and the platform's
# default certificate locations that `SSL_CERT_FILE` and `SSL_CERT_DIR` override. The methods of an
# `SSLContext` that load them (`load_default_certs`, `set_default_verify_paths`) are on an object
# the analysis cannot resolve; the module-level functions that do the same are judged here.
_FOUND = (
    g(
        "netrc",
        "netrc",
        (K.HOME, K.PID),
        "reads `~/.netrc` unless it is given the file, and refuses the default file when another "
        "user owns it: the comparison reads the process's user id",
        unless=later(0, "file"),
    ),
    g("netrc", "NetrcParseError", None, TYPE),
    g(
        "ssl",
        "get_default_verify_paths",
        K.ENVIRON,
        "the default certificate file and directory, taken from SSL_CERT_FILE and SSL_CERT_DIR "
        "before the OpenSSL build's own locations",
    ),
    g(
        "ssl",
        "create_default_context",
        K.ENVIRON,
        "loads the default certificate locations (SSL_CERT_FILE, SSL_CERT_DIR) unless it is given "
        "a `cafile`; a call that names only a `capath` or `cadata` is recorded too, which "
        "over-records",
        unless=Unless("cafile", None),
    ),
    g(
        "ssl",
        "RAND_bytes RAND_pseudo_bytes RAND_status",
        K.RANDOM,
        "draws from, or asks the state of, the OpenSSL random generator",
    ),
    g(
        "ssl",
        "RAND_add",
        None,
        "mixes the caller's bytes into the generator: a write, and it acquires nothing",
    ),
    g("ssl", "create_connection get_server_certificate wrap_socket", None, NET),
    g("ssl", "enum_certificates enum_crls", None, FILES),
    g(
        "ssl",
        "DER_cert_to_PEM_cert PEM_cert_to_DER_cert cert_time_to_seconds get_protocol_name "
        "match_hostname",
        None,
        VALUES,
    ),
    g(
        "ssl",
        "AlertDescription CertificateError DefaultVerifyPaths MemoryBIO Options Purpose "
        "SSLCertVerificationError SSLContext SSLEOFError SSLError SSLErrorNumber SSLObject "
        "SSLSession SSLSocket SSLSyscallError SSLWantReadError SSLWantWriteError "
        "SSLZeroReturnError TLSVersion VerifyFlags VerifyMode",
        None,
        TYPE,
    ),
    g(
        "ssl",
        "ALERT_DESCRIPTION_* CERT_* CHANNEL_BINDING_TYPES HAS_* OP_* PEM_FOOTER PEM_HEADER "
        "PROTOCOL_* SO_TYPE SOCK_STREAM SOL_SOCKET SSL_ERROR_* VERIFY_*",
        None,
        CONSTANT,
    ),
    g(
        "ssl",
        "OPENSSL_VERSION OPENSSL_VERSION_INFO OPENSSL_VERSION_NUMBER get_sigalgs",
        None,
        BUILD,
    ),
    g(
        "ssl",
        "namedtuple socket socket_error",
        None,
        "a name imported for the module's own use, or an alias of a built-in error type",
    ),
)

# meiva:note
# Lookups of the environment that a library call hides: where a shared library is searched for
# (`LD_LIBRARY_PATH`, `PATH`), and which browser opens a page (`BROWSER`, `DISPLAY`,
# `WAYLAND_DISPLAY`, `TERM`, `PATH`). The call itself starts a process, an effect the purity layer
# judges; the lookup is the part that reads the process's own environment.
_LOOKUPS = (
    g(
        "ctypes.util",
        "find_library test",
        K.ENVIRON,
        "searches for a shared library along LD_LIBRARY_PATH and PATH (`test` is a demonstration "
        "that calls it)",
    ),
    g("ctypes.util", "find_msvcrt", None, "names the runtime of a Windows build and reads nothing"),
    g(
        "ctypes.util",
        "dllist",
        K.PID,
        "the shared libraries loaded into the process: what the process says about itself",
    ),
    g(
        "webbrowser",
        "get open open_new open_new_tab register register_standard_browsers register_X_browsers",
        K.ENVIRON,
        "chooses the browser from BROWSER, DISPLAY, WAYLAND_DISPLAY, TERM and PATH the first time "
        "any of them is used",
    ),
    g(
        "webbrowser",
        "main",
        (K.ARGV, K.ENVIRON),
        "the command-line entry of the module: reads sys.argv and opens the page in the browser "
        "the environment chooses",
    ),
    g(
        "webbrowser",
        "BackgroundBrowser BaseBrowser Chrome Chromium Edge Elinks Epiphany Error GenericBrowser "
        "Galeon Grail Konqueror MacOSX MacOSXOSAScript Mozilla Opera UnixBrowser WindowsDefault",
        None,
        TYPE,
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
        *_FED,
        *_FOUND,
        *_LOOKUPS,
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
