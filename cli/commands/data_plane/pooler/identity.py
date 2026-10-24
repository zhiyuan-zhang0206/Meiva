# meiva:note
# Which configuration file a running pgbouncer was started with, read from its command line.
#
# The configuration file is the one argument that names a home, so it is how a pooler is
# attributed to a home. Both halves are load-bearing, and which one carries the path is a
# platform difference rather than a fallback. Measured on both:
#
#     Linux, apt pgbouncer 1.18   argv keeps the absolute ini path;
#                                 cwd stays wherever the launcher stood
#     macOS, brew pgbouncer 1.25  argv is rewritten to a bare `pgbouncer.ini`;
#                                 the process chdir()s into the config dir
#
# Neither field alone identifies the pooler on both. Resolving the argument against the
# working directory when it is relative is the single rule that reads both, and it is
# exactly the resolution pgbouncer performed to find its own config. The result is compared
# as a resolved path, never as a substring: `~/.meiva` is a prefix of `~/.meiva-preview`, and
# matching pooler processes by substring is how a co-located cluster's tooling reaches the
# wrong home (what `pkill -f pgbouncer.ini` did to production on 2026-08-06).

from __future__ import annotations

import posixpath
from collections.abc import Sequence

__all__ = ["anchored_ini", "ini_argument"]


# meiva:note
# The `-d <ini>` argument of a pgbouncer command line, or None when there is none.
def ini_argument(argv: Sequence[str]) -> str | None:
    if "-d" not in argv:
        return None
    position = argv.index("-d") + 1
    return argv[position] if position < len(argv) else None


# meiva:note
# The argument anchored at the process's working directory when it is relative.
def anchored_ini(argument: str, cwd: str) -> str:
    return argument if posixpath.isabs(argument) else posixpath.join(cwd, argument)
