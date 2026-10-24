# meiva:note
# Which verbs open the loguru sinks a service process has, and under which name.

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["START_SINK", "cli_log_name"]

START_SINK = "cli-start"


# meiva:note
# The sink name the verb in `args_in` opens before its dispatch, or None for a verb that opens none.
#
# The verbs that bring this unit up (every in-process `cmd_start`) open the loguru
# sinks a service process has, under these names. Importing `base.log` drops loguru's
# default handler, so without them every record the start path writes only through
# loguru is discarded: a skipped pgvector pre-create, untracked migration files that
# will not be applied. `init_cli_process` adds stderr, `$MEIVA_HOME/logs/<name>.log`
# and the event pipeline, and emits no `service_started` row. Other verbs print to the
# caller's terminal and open none: the event pipeline would carry one row per
# `meiva status`.
#
# Opening them builds Settings, so `meiva start` opens its sink only after the home is
# admitted, `meiva init` opens none (it is Settings-free), and every other verb opens
# its sink after the home gates, right before dispatch. A Settings failure raised while
# opening is the one the command would raise, and reaches the same handlers.
def cli_log_name(args_in: Sequence[str]) -> str | None:
    match tuple(args_in[:2]):
        case ("start", *_):
            return START_SINK
        case ("restart", *_):
            return "cli-restart"
        case ("lgtm", "on" | "off"):
            return "cli-lgtm"
        case _:
            return None
