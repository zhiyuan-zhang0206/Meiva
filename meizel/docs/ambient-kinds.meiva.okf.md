---
type: doc
title: "meizel/ — Ambient Kinds"
description: "The closed vocabulary of the standard library's ambient kinds — `argv`, `environ`, `clock`, `home`/`cwd`, `platform`, `pid`, `random` — the state kinds a repository's policy adds, the operating-system channels the standard-library kinds cover, the table that judges every name of those channels, and the guard that keeps the table closed."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Kinds

The standard library's kinds are a closed vocabulary (`AmbientKind` in `meizel/graph/model.py`). The
standard-library names that acquire each kind are judged in one table,
`meizel/graph/ambient_surface.py` (the groups of the process channels `os`, `sys`, `time` and
`datetime` are in `ambient_process_table.py`, the shape of a judgment in `ambient_judgments.py`).
The state a repository keeps itself is not in the vocabulary: each repository declares its own kinds
as the keys of the policy table `[ambient]` (`Policy.state_kinds`), and a kind of either family
travels as a plain name (`KindName`). A manifest's `[state]` accepts the declared kinds and `[roots]`
accepts those and the standard library's; any other kind is `manifest-syntax`. Diff hints read the
same two sets (the standard library's and the policy's at the revision they judge) and reject any
other root kind.

| kind | what it is | acquired by |
|---|---|---|
| `argv` | the command line the process was started with | `sys.argv`, `sys.orig_argv`, `sys.flags`, `sys.warnoptions`; `parser.parse_args()` (and `parse_known_args`, `parse_intermixed_args`) called with no `args`, or with a parameter of the same function that defaults to `None` and is not rebound (`def main(argv=None): parser.parse_args(argv)`: argparse reads `sys.argv[1:]` itself when handed `None`); `fileinput.input()` and `FileInput()` with no `files` |
| `environ` | the environment and what the operating system derives from it | `os.environ`, `os.getenv`, `os.putenv`, `os.path.expandvars`, `os.get_exec_path`, `shutil.which` without a `path`, `tempfile.gettempdir` and the temporary-file constructors without a `dir`, `locale.getlocale` and the other locale readers, `ssl.get_default_verify_paths`, `ssl.create_default_context` without a `cafile` |
| `clock` | the instant and the host's time zone | `time.time[_ns]`, `datetime.now/utcnow/today`, `date.today`, `time.localtime/ctime/mktime`, `time.tzname/timezone/altzone/daylight`, `datetime.fromtimestamp` without a zone, `.astimezone()` without a zone, `time.gmtime/asctime/strftime` without a time value, `email.utils.formatdate` (every call), `email.utils.localtime`; `time.monotonic`, `time.perf_counter`, `time.process_time`, `time.thread_time`, `os.times` (and `_ns`) and `loop.time()` only in a function that also waits |
| `home`, `cwd` | where the user's files are, and where a relative path starts | `Path.home`, `os.path.expanduser`, `.expanduser()`, `site.getusersitepackages`, `netrc.netrc()` without a file; `os.getcwd`, `Path.cwd`, `os.path.abspath`, `os.path.relpath` without a `start`, `Path.absolute()`, `os.listdir()`/`os.scandir()` without a path |
| `platform` | which machine and which platform this is | `sys.platform`, `os.name`, `os.uname`, `os.cpu_count`, `os.getloadavg`, `os.sysconf`, `socket.gethostname`, `uuid.getnode`, `multiprocessing.cpu_count`, `time.get_clock_info`, `platform.*` except the `python_*` functions, which describe the pinned build |
| `pid` | what the process says about itself: its ids, its interpreter, its terminal | `os.getpid` and the other id readers, `sys.executable`, `sys.prefix`, `sys.stdin`, `input()`, `isatty`, `shutil.get_terminal_size`, `getpass.getuser`, `os.getlogin`, `pwd`/`grp` lookups, `threading.get_ident`, `psutil.Process()` without a pid |
| `random` | entropy | `random.*` (literal non-None seeded generators and class references excluded), `secrets.*` (not `secrets.compare_digest`), `uuid.uuid1`, `uuid.uuid4`, `email.utils.make_msgid`, `ssl.RAND_bytes`, `os.urandom`, `os.getrandom` |
| a state kind of the policy's `[ambient]` | the state the repository keeps itself | the names that table lists for it, resolved against the modules whose `[state]` declares the kind ([[meizel/docs/checker/ambient.meiva.okf.md|the ambient reads node]]) |

## The channels

A process cannot know anything it was not told except through a few operating-system channels, so
the vocabulary is closed by listing the channels rather than by listing the spellings that someone
met. Each channel is either an ambient kind or the concern of another layer:

| channel | judged as |
|---|---|
| arguments the process was started with | `argv` |
| the environment variables, and what is derived from them (the search path, the temporary directory, the locale) | `environ` |
| the process's own identity: ids, interpreter, login, which standard streams it is wired to (the stream, not its contents, which are an effect), terminal | `pid` |
| the host's identity: platform, name, processor count, load, hardware address | `platform` |
| the clock and the host's time zone | `clock` |
| entropy | `random` |
| the anchors a relative path hangs from: the home directory, the working directory | `home`, `cwd` |
| the contents and metadata of files | not a kind: an effect, `impure-call` in the purity layer |
| the network, including name resolution | not a kind: an effect, `impure-call` |
| the repository's own state | a state kind the repository's policy declares |

A name that only converts, compares or formats the values it is given reads none of these.

## What a call that reads the world depends on

An interval clock read for a deadline, a duration or a TTL is the plain use of `time`; it is a
window onto the world only where the same function waits, because then how long to wait next is
decided in a loop. A function waits when it calls a name that contains `sleep` (`time.sleep`,
`asyncio.sleep`, `timers.asleep`, `retry_sleep`) or a function of its module that does, however
the helper is named (`_pause()` that sleeps). A function without parameters that is only
`return time.monotonic()` is a clock under another name: a call of it in a function that waits is
the reading, recorded as `_monotonic()`. `loop.time()` on `get_running_loop()` or a `loop`,
`_loop` or `event_loop` is an interval clock. The wall clock is always one.

A call that reads the world only when it is given nothing (`time.gmtime()`, `time.strftime(fmt)`,
`psutil.Process()`, `tempfile.mkdtemp()` without a `dir`) is an acquisition then, and describes a
value it was handed otherwise.

A `random.Random` constructor has an independent seed only when its positional seed or `x=`
argument is a non-None literal, including zero or an empty string/bytes value. Missing seeds,
literal `None`, dynamic seeds and unbounded argument expansion conservatively acquire `random`:
a dynamic value can be `None`, and the checker does not infer its runtime value or type.
This is static evidence of a possible ambient
seed, not proof that every execution reads the machine. A class reference alone acquires nothing.

The names that acquire each kind are judged one by one, and the table is kept closed:
[[meizel/docs/ambient-surface.meiva.okf.md|the ambient surface node]].

Parent: [[meizel/docs/checker/ambient.meiva.okf.md|the ambient reads rules]].
