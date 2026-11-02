---
type: doc
title: "meizel/ — Ambient Surface: Calls That Read Several Channels"
description: "The names of the ambient surface table that list more than one kind, the kinds of each and the source that shows it, and the names that were looked at and left with one kind."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Surface: Calls That Read Several Channels

## How an entry lists them

A judgment lists the kinds of one call. Most names read one channel and list one kind. A call that
reads several lists them all, as a tuple in `g(...)`: `(K.ENVIRON, K.PID)` for `getpass.getuser`,
`(K.CLOCK, K.PID, K.RANDOM, K.PLATFORM)` for `email.utils.make_msgid`, `(K.ENVIRON, K.RANDOM)` for
`tempfile.mkdtemp`. A tuple names at least two different kinds (a one-kind judgment is the kind
itself, and a repeated kind is refused when the table is built), and a judgment with no kind is
the reason the name reads nothing. Many entries listing many kinds is the point: the table used
to record one kind per name, so a caller that paid back the one it recorded (the random draw of
`make_msgid`) still read the clock, the process id and the host name, and nothing was on its
books.

A call that lists several kinds is several acquisitions at the same line and the same target, one
per kind. The debt key `file#kind target` is unchanged, so one call is several keys
(`file#environ tempfile.mkdtemp()` and `file#random tempfile.mkdtemp()`, each with the count of its
calls), and a root has to declare every kind of the name it calls before the file stops owing
any of them. Paying such a debt means removing the call, or handing it the value that spares it,
which removes all of its keys at once.

## The names

These names list more than one kind (see [[meizel/docs/ambient-surface.meiva.okf.md|the ambient
surface node]] for how); each was read in the source of the pinned interpreter (3.12) unless the
name does not exist there, and a read counts when it is part of the ordinary outcome of the call and
not of an error path. The kinds come from what the call does, never from what its result is used for.

| name | kinds | why |
|---|---|---|
| `email.utils.make_msgid` | `clock`, `pid`, `random`, `platform` | the id is built from `time.time()`, `os.getpid()`, a random 64-bit number and, without a `domain`, `socket.getfqdn()`; a `domain` spares only the host name, and the `unless` clause would spare all four, so there is none and the host name is over-recorded for a call that gives a `domain` |
| `tempfile.mkdtemp`, `mkstemp`, `mktemp`, `TemporaryDirectory`, `NamedTemporaryFile`, `TemporaryFile`, `SpooledTemporaryFile` | `environ`, `random` | the directory comes from `TMPDIR` and the name is drawn from a random generator (`TemporaryFile` on a platform with anonymous files and `SpooledTemporaryFile` until it rolls over draw none, and are judged by the call that can); `dir` spares the call as a whole, so a call that names its directory still draws a random name and records nothing |
| `getpass.getuser` | `environ`, `pid` | `LOGNAME`, `USER`, `LNAME` or `USERNAME`, and when none is set (a launchd job, cron) the account database entry of `os.getuid()` |
| `shutil.get_terminal_size` | `environ`, `pid` | `COLUMNS` and `LINES`, else the terminal `sys.__stdout__` is attached to |
| `uuid.uuid1`, `uuid.uuid6` | `clock`, `platform`, `random` | the current time, the host's hardware address (`getnode`) and a random clock sequence; `uuid7` is `clock` and `random`, `uuid4` and `uuid8` are `random` (3.12 has no `uuid6`, `uuid7` or `uuid8`; their kinds follow the 3.14 documentation) |
| `uuid.getnode` | `platform`, `random` | the hardware address, and a random 48-bit number when the host yields none, which a container often does |
| `uuid.main`, `webbrowser.main` | `argv` and what the command line asks for | the command-line entries of the modules |
| `site.getuserbase`, `getusersitepackages`, `USER_BASE`, `USER_SITE`, `gethistoryfile` | `environ`, `home` | `PYTHONUSERBASE` (`PYTHON_HISTORY` for the history file), else a path under the home directory |
| `site.check_enableusersite`, `ENABLE_USER_SITE` | `environ`, `argv`, `pid` | the `-s` flag (which `PYTHONNOUSERSITE` sets) and whether the real and effective user and group ids agree |
| `sysconfig.get_path`, `get_paths`, `get_config_var`, `get_config_vars` | `pid`, `environ`, `home` | all four go through `get_config_vars()`, whose first call runs `_init_config_vars()`: it computes the user base for every variable (`PYTHONUSERBASE`, else the home directory), derives the prefixes, and on macOS reads `CC`, `CFLAGS` and `ARCHFLAGS` |
| `sysconfig.get_platform` | `platform`, `environ` | `_PYTHON_HOST_PLATFORM` (a cross-compilation override) and, on macOS, `ARCHFLAGS` take precedence over what the machine reports |
| `fileinput.input`, `FileInput` | `argv`, `pid` | `sys.argv[1:]`, and standard input when there are no files; `files` spares the call |
| `netrc.netrc` | `home`, `pid` | `~/.netrc`, and the default file is refused when another user owns it, which reads `os.getuid()`; `file` spares the call |
| `locale.getpreferredencoding` | `environ`, `argv` | the locale from the environment, unless the interpreter was started in UTF-8 mode (`-X utf8`), which answers first |

Looked at and left with one kind: `tempfile.gettempdir` (the working directory is the last resort
of its search, reached only when no system directory is writable), `shutil.which` (a missing `PATH`
falls back to a build constant), `ssl.create_default_context` (`-E` only gates the environment read it
already lists), `os.path.expanduser` and `Path.home` (the account
database entry of the user id is the same channel as `HOME`), `psutil.Process` (its creation time is
the process's own, not the clock), and `platform.architecture`, `platform.libc_ver` and
`platform.platform`, which inspect the interpreter's own executable to describe the machine.

## What a clause spares

An `unless` clause spares the call as a whole, every kind of the entry. That is a decision of this
table, not an oversight, and it leaves these gaps:

- `tempfile.mkdtemp(dir=...)` and the other temporary-file constructors with a `dir` draw a random name
  and record nothing. The caller has no argument that could hand that name in, and the name does not
  shape the caller's decisions (where the file lands is the `dir`, which the caller named). These
  calls are outside the debt of #8237: there is nothing to repay, because no parameter removes the draw.
- `make_msgid(domain=...)` records the host name although a `domain` spares it. The entry has no clause
  (one would spare the clock, the process id and the random number with it), so the over-record
  stays; it errs towards recording.
- `uuid.uuid1(node=..., clock_seq=...)` with both given still reads the clock and records the hardware
  address and the random clock sequence it does not read; `fileinput.input(files=...)` records nothing
  although `files=['-']` reads standard input.

Parent: [[meizel/docs/ambient-surface.meiva.okf.md|the ambient surface node]].
