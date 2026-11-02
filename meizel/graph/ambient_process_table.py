# meiva:note
# The judgments of the process channels: the `os` and `os.path` modules, `sys`, `time` and
# `datetime`. The host, identity, entropy and anchor channels are in `ambient_surface.py`.

from __future__ import annotations

from meizel.graph.ambient_judgments import (
    BUILD,
    CONSTANT,
    FILES,
    STATE,
    SYNTAX,
    TYPE,
    VALUES,
    Group,
    K,
    g,
    later,
)

__all__ = ["PROCESS_GROUPS"]

# meiva:note
# The `os` module: arguments to the process are not here (they are `sys.argv`); its environment, its
# identity, the host and the working directory are; the rest is file, descriptor and process
# operations or constants.
_OS = (
    g(
        "os",
        "environ environb getenv getenvb putenv unsetenv reload_environ get_exec_path",
        K.ENVIRON,
        "the process environment, read or changed; `get_exec_path` reads PATH",
    ),
    g("os", "getcwd getcwdb", K.CWD, "the working directory"),
    g(
        "os",
        "listdir scandir",
        K.CWD,
        "lists the working directory when it is given no path",
        unless=later(0, "path"),
    ),
    g(
        "os",
        "name uname cpu_count process_cpu_count getloadavg sysconf confstr sched_getaffinity "
        "supports_bytes_environ supports_dir_fd supports_effective_ids supports_fd "
        "supports_follow_symlinks",
        K.PLATFORM,
        "which platform and machine this is: its name, size, load and the features it supports",
    ),
    g(
        "os",
        "getpid getppid getpgrp getuid geteuid getgid getegid getgroups getgrouplist getlogin "
        "getresuid getresgid getpriority sched_getparam sched_getscheduler sched_rr_get_interval "
        "umask ctermid ttyname isatty get_terminal_size device_encoding",
        K.PID,
        "what the process says about itself: its ids, its scheduling, its terminal; `umask` is read "
        "by setting it",
    ),
    g("os", "urandom getrandom", K.RANDOM, "draws from operating-system entropy"),
    g(
        "os",
        "times",
        K.CLOCK,
        "process times with the elapsed real time: an interval clock",
        interval=True,
    ),
    g("os", "sep altsep extsep pathsep linesep curdir pardir devnull defpath", None, SYNTAX),
    g(
        "os",
        "O_* P_* SEEK_* EX_* W* CLD_* F_* PRIO_* RTLD_* SCHED_* POSIX_SPAWN_* POSIX_FADV_* ST_* "
        "AT_* CLONE_* EFD_* GRND_* MFD_* PIDFD_* RWF_* SF_* SPLICE_F_* STATX_* TFD_* XATTR_* "
        "R_OK W_OK X_OK NGROUPS_MAX TMP_MAX NODEV",
        None,
        CONSTANT,
    ),
    g(
        "os",
        "DirEntry GenericAlias Mapping MutableMapping PathLike error stat_result statvfs_result "
        "terminal_size times_result uname_result waitid_result sched_param statx_result",
        None,
        TYPE,
    ),
    g(
        "os",
        "fsencode fsdecode fspath strerror waitstatus_to_exitcode major minor makedev "
        "confstr_names sysconf_names pathconf_names",
        None,
        "converts a value it is given, or is a table of names: it reads nothing",
    ),
    g(
        "os",
        "abort access chdir chflags chmod chown chroot close closerange copy_file_range dup dup2 "
        "eventfd eventfd_read eventfd_write execl execle execlp execlpe execv execve execvp execvpe "
        "fchdir fchmod fchown fdatasync fdopen fork forkpty fpathconf fstat fstatvfs fsync ftruncate "
        "fwalk get_blocking get_handle_inheritable get_inheritable getpgid getsid getxattr grantpt "
        "initgroups kill killpg lchflags lchmod lchown link listdrives listmounts listvolumes lockf "
        "login_tty lseek lstat makedirs memfd_create mkdir mkfifo mknod nice open openpty "
        "pathconf pidfd_open pipe pipe2 plock popen posix_fadvise posix_fallocate posix_openpt "
        "posix_spawn posix_spawnp pread preadv ptsname pwrite pwritev read readinto readlink "
        "readv register_at_fork remove removedirs removexattr rename renames replace rmdir "
        "sched_get_priority_max sched_get_priority_min sched_setaffinity sched_setparam "
        "sched_setscheduler sched_yield sendfile set_blocking set_handle_inheritable "
        "set_inheritable setegid seteuid setgid setgroups setns setpgid setpgrp setpriority "
        "setregid setresgid setresuid setreuid setsid setuid setxattr spawnl spawnle spawnlp "
        "spawnlpe spawnv spawnve spawnvp spawnvpe splice startfile stat statvfs statx symlink "
        "sync system tcgetpgrp tcsetpgrp timerfd_create timerfd_gettime timerfd_gettime_ns "
        "timerfd_settime timerfd_settime_ns truncate unlink unlockpt unshare utime wait wait3 wait4 "
        "waitid waitpid walk write writev add_dll_directory listxattr",
        None,
        FILES + "; `getpgid` and `getsid` ask the process table about a pid the caller names",
    ),
)

_OS_PATH = (
    g("os.path", "expanduser", K.HOME, "the user's home directory"),
    g("os.path", "expandvars", K.ENVIRON, "expands names from the environment"),
    g("os.path", "abspath", K.CWD, "anchors a relative path at the working directory"),
    g(
        "os.path",
        "relpath",
        K.CWD,
        "relative to the working directory when it is given no start",
        unless=later(1, "start"),
    ),
    g(
        "os.path",
        "realpath",
        None,
        "resolves symlinks, a read of the file system that the purity layer judges (`impure-call`); "
        "it anchors a relative path at the working directory, but nearly every use canonicalises a "
        "path that is already absolute, and the relative one was read where it was made",
    ),
    g(
        "os.path",
        "exists lexists isfile isdir islink isjunction ismount isdevdrive getsize getatime "
        "getmtime getctime samefile sameopenfile samestat",
        None,
        FILES,
    ),
    g(
        "os.path",
        "join split splitext splitdrive splitroot basename dirname normcase normpath isabs "
        "commonpath commonprefix supports_unicode_filenames ALLOW_MISSING ALL_BUT_LAST",
        None,
        VALUES,
    ),
    g("os.path", "sep altsep extsep pathsep curdir pardir devnull defpath", None, SYNTAX),
)

# meiva:note
# The `sys` module: what the process was started with, what it says about itself, and its streams.
_SYS = (
    g(
        "sys",
        "argv orig_argv flags warnoptions _xoptions",
        K.ARGV,
        "what the process was started with: its argument vector and its interpreter options",
    ),
    g(
        "sys",
        "executable prefix base_prefix exec_prefix base_exec_prefix stdout.isatty stderr.isatty",
        K.PID,
        "what the process says about itself: the interpreter it runs on and what its standard "
        "streams are attached to",
    ),
    g(
        "sys",
        "stdin __stdin__",
        K.PID,
        "names the stream the parent attached as this process's input: how the process is wired is "
        "its own statement, while reading the contents is an effect on a descriptor that the purity "
        "layer judges (`impure-call`); the repair is to take the stream as a value from the entry",
    ),
    g(
        "sys",
        "platform getwindowsversion getandroidapilevel winver dllhandle",
        K.PLATFORM,
        "which platform this is",
    ),
    g(
        "sys",
        "getfilesystemencoding getfilesystemencodeerrors",
        K.ENVIRON,
        "the file-system encoding the interpreter took from the locale of the environment",
    ),
    g(
        "sys",
        "version version_info hexversion implementation api_version abiflags byteorder maxsize "
        "maxunicode float_info int_info hash_info thread_info float_repr_style copyright "
        "builtin_module_names stdlib_module_names platlibdir abi_info getdefaultencoding "
        "is_finalizing is_remote_debug_enabled",
        None,
        BUILD,
    ),
    g(
        "sys",
        "stdout stderr __stdout__ __stderr__",
        None,
        "an output stream: writing is an effect, and the ambient rules govern what a process takes in; "
        "what the streams are attached to is listed above (`isatty`)",
    ),
    g(
        "sys",
        "path modules meta_path path_hooks path_importer_cache dont_write_bytecode "
        "pycache_prefix get_int_max_str_digits set_int_max_str_digits getrecursionlimit "
        "setrecursionlimit getswitchinterval setswitchinterval intern getsizeof getrefcount "
        "getallocatedblocks getunicodeinternedsize gettotalrefcount getdlopenflags setdlopenflags "
        "call_tracing settrace gettrace setprofile getprofile addaudithook audit breakpointhook "
        "displayhook excepthook unraisablehook UnraisableHookArgs exc_info exception exit last_exc "
        "last_traceback last_type last_value ps1 ps2 tracebacklimit monitoring remote_exec "
        "activate_stack_trampoline deactivate_stack_trampoline is_stack_trampoline_active "
        "get_asyncgen_hooks set_asyncgen_hooks get_coroutine_origin_tracking_depth "
        "set_coroutine_origin_tracking_depth get_lazy_imports get_lazy_imports_filter lazy_modules "
        "set_lazy_imports set_lazy_imports_filter",
        None,
        STATE,
    ),
)

# meiva:note
# Time: the wall clock and the host's time zone are ambient; the interval clocks are ambient only
# in a function that waits; sleeping is an effect.
_TIME = (
    g("time", "time time_ns clock_gettime clock_gettime_ns", K.CLOCK, "the wall clock"),
    g(
        "time",
        "localtime ctime mktime timezone altzone daylight tzname",
        K.CLOCK,
        "the current time or the host's time zone: the same instant reads differently on another host",
    ),
    g(
        "time",
        "gmtime",
        K.CLOCK,
        "the current time when it is given no seconds",
        unless=later(0, "secs"),
    ),
    g(
        "time",
        "asctime",
        K.CLOCK,
        "the current local time when it is given no time",
        unless=later(0, "t"),
    ),
    g(
        "time",
        "strftime",
        K.CLOCK,
        "the current local time when it is given no time",
        unless=later(1, "t"),
    ),
    g(
        "time",
        "monotonic monotonic_ns perf_counter perf_counter_ns process_time process_time_ns "
        "thread_time thread_time_ns",
        K.CLOCK,
        "an interval clock: a deadline or a duration is the plain use of `time`; the read is a "
        "decision only where the same function waits",
        interval=True,
    ),
    g(
        "time",
        "sleep tzset clock_settime clock_settime_ns",
        None,
        "waits or changes the process's clock settings: an effect",
    ),
    g(
        "time",
        "get_clock_info clock_getres pthread_getcpuclockid",
        K.PLATFORM,
        "asks the host about its clocks: their resolution and implementation, or the CPU-time clock "
        "of a thread",
    ),
    g("time", "strptime struct_time", None, VALUES),
    g("time", "CLOCK_*", None, CONSTANT),
)

_DATETIME = (
    g(
        "datetime",
        "datetime date time timedelta timezone tzinfo UTC MINYEAR MAXYEAR datetime_CAPI",
        None,
        "a type or constant; the readers among the members of `datetime` and `date` are listed "
        "below, every other member converts, compares or formats the values it is given",
    ),
    g("datetime.datetime", "now utcnow today", K.CLOCK, "the wall clock"),
    g(
        "datetime.datetime",
        "fromtimestamp",
        K.CLOCK,
        "converts to the host's time zone when it is given no zone",
        unless=later(1, "tz"),
    ),
    g("datetime.date", "today", K.CLOCK, "the wall clock"),
    g(
        "datetime.date",
        "fromtimestamp",
        K.CLOCK,
        "converts to the host's time zone, which a date cannot be given",
    ),
    g("datetime.datetime", "*", None, VALUES),
    g("datetime.date", "*", None, VALUES),
    g("datetime.time", "*", None, VALUES),
)


PROCESS_GROUPS: tuple[Group, ...] = (*_OS, *_OS_PATH, *_SYS, *_TIME, *_DATETIME)
