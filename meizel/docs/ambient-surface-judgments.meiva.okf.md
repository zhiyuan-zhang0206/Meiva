---
type: doc
title: "meizel/ — Ambient Surface Judgments"
description: "The judgments of the ambient surface table that differ from the obvious spelling (resolve and realpath as a known blind spot, stdin, the clock readers, the interpreter build), the standard-library modules inspected and left out, and the forms of reading that the analysis cannot see."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Surface Judgments

## Judgments that differ from the obvious spelling

- `Path.resolve` and `os.path.realpath` are a **known blind spot**, not a judgment that they read
  nothing. Both anchor a relative receiver at the working directory, and the analysis has no
  types to tell a relative receiver from an absolute one. `Path('').resolve()`,
  `os.path.realpath('.')` and `[Path(a).resolve() for a in argv]` read the working directory and
  record nothing (a test pins this, so the sentence stays true). The last form is the real cost:
  a path that came from the command line is relative, and a dozen lint scripts resolve exactly
  those (`scripts/lint/code_structure.py`, `scripts/lint/fixture_scope.py`,
  `meizel/language/no_script_sibling_imports.py` and others). The table treats both names as `None`
  only because a recorded `cwd` on every `resolve()` of an absolute path (about 560 sites in 290
  files) would bury the few that matter; the purity layer already judges them as file-system
  reads. `os.path.abspath` is different only because its purpose is to anchor: it is `cwd`, also
  on an absolute literal. A fix needs types (is the receiver a parameter, a literal, an `argv`
  element), not a table entry.
- `tempfile.gettempprefix` returns the constant `tmp` and reads nothing. `tempfile.gettempdir`
  and a temporary file created without a `dir` read `TMPDIR`.
- `time.localtime(secs)`, `time.ctime(secs)`, `time.mktime`, `email.utils.localtime` and
  `email.utils.mktime_tz` are `clock` even when given a value: the same instant reads differently
  in another zone. `time.gmtime(secs)` does not.
- `time.get_clock_info`, `time.clock_getres` and `time.pthread_getcpuclockid` are `platform`: they
  ask the host about its clocks (resolution, implementation, a thread's CPU-time clock) and are not
  conversions of a given value.
- `os.sep`, `os.linesep`, `sys.byteorder`, `sys.version_info`, `sys.version` and
  `platform.python_version` (with the other `platform.python_*` functions, which parse the same
  version string) format paths or describe the interpreter build the deployment pins; they do not
  identify the host and are not read. `sys.platform`, `os.name` and the other `platform` functions
  do, and are `platform`.
- `sys.stdin`, `sys.__stdin__` and `input()` are `pid`, and the reason is the stream, not the
  text: the name says which stream the parent attached to this process, which is how the process
  is wired, like `isatty`. What a line of it contains is an effect on a descriptor, which the
  purity layer judges. The repair is the same either way: the entry takes the stream (or the typed
  answer) and passes it down as a value.
- `fileinput.input()` and `FileInput()` are `argv` unless they are given `files`: they read the
  files named in `sys.argv[1:]`. `site.getuserbase`, `getusersitepackages` and `USER_SITE` are
  `home`; `email.utils.formatdate` without a time is `clock`; `email.utils.make_msgid` is `random`.
- `sys.path` and `sys.modules` are the interpreter's own mutable state: they are written far more
  often than they inform a decision.
- `pwd` and `grp` lookups are `pid`: they answer who a uid is on this host, and the lookup of the
  process's own user is the common use.
- Only the standard library's channel modules are closed. A third-party reader (`psutil.Process`)
  is listed when the repository uses it as a reader, and TypeScript acquisition has its own
  spellings ([[meizel/docs/checker/typescript.meiva.okf.md|the TypeScript node]]).

## Inspected modules that are not judged

A module is left out when its names are effects (the purity layer's), state the caller built, or
functions of their inputs. The list is what was looked at, so that a gap is a recorded choice and
not an oversight; a module nobody has looked at is not on it.

| module | why it is not a channel module |
|---|---|
| `subprocess` | every call is an effect that starts a child; the program it starts is named by the caller (`sys.executable` and `shutil.which`, which are judged) |
| `signal` | installs handlers and sends signals (effects); `getsignal` reads a handler the caller installed |
| `argparse`, `optparse` | `parse_args()` without an argument list is judged by the method name (`METHOD_READS`), since the receiver has no type; `getopt` takes its list as a parameter |
| `warnings`, `logging`, `atexit`, `gc` | process-wide hooks and sinks: writes, not reads |
| `inspect`, `traceback`, `faulthandler` | introspection of the program's own call stack |
| `ctypes`, `mmap`, `fcntl`, `select` | descriptor and foreign-call effects |
| `termios`, `tty`, `pty`, `readline`, `curses` | change the terminal; which terminal it is, is `isatty`, which is judged |
| `builtins` (other than `input`) | `open` is the purity layer's; the rest convert or build values |

Not inspected: third-party readers beyond `psutil.Process` and the TypeScript side.

## Forms the analysis cannot see

The table judges a name where it is used. These forms hide the name from it, and each is a known gap
rather than a judgment:

- A reader named but not called, then called elsewhere: `map(shutil.which, names)`,
  `functools.partial(tempfile.mkdtemp)`. An entry with an `unless` clause is judged at the call, and
  a reference alone acquires nothing.
- A third-party reader that is not listed: `psutil.cpu_count()` reads the host's processor count
  like `os.cpu_count()` and is not recorded.
- A naive `datetime(...).timestamp()` converts in the host's time zone and is not recorded: the
  method table names `astimezone`, not `timestamp`, which an aware datetime also has.
- A private name, such as `tempfile._get_default_tempdir()`, is outside the table unless an entry
  lists it.
- A relative path resolved by `resolve()` or `realpath` (the blind spot above).

Parent: [[meizel/docs/ambient-surface.meiva.okf.md|the ambient surface node]].
