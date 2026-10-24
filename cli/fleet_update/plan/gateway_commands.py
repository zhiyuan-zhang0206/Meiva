# meiva:note
# The commands the window runs against the cluster as a whole: the gateway's roster and smoke
# programs (shipped over stdin), the packages refresh and the drift checks on each host, the
# machine name each host boots into, and the pre-update check of the stored schedule table.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import shlex

from .host_commands import HOME, MEIVA, PROGRAM, PYTHON


def roster_command() -> str:
    return f"{PROGRAM} roster"


def smoke_command(machine: str, timeout_s: int) -> str:
    return f"{PROGRAM} smoke {shlex.quote(machine)} {timeout_s}"


# meiva:note
# The command that prints the name this host boots into. The roster lists machines by that name,
# which is not the SSH alias the operator typed.
def machine_name_command() -> str:
    return (
        f'{PYTHON} -c "from base.config import ConfigBoot; '
        "from base.cluster.machine.boot import machine_from_boot; "
        'print(machine_from_boot(ConfigBoot()).name())"'
    )


def refresh_command() -> str:
    return f"{HOME}; {MEIVA} packages refresh"


def drift_command(verb: str) -> str:
    return f"{HOME}; {MEIVA} {verb}"


# meiva:note
# NEW's offline check of the stored schedule table: the rows are read under the host's OLD
# checkout and checked under a throwaway worktree of NEW, from a template the gateway runs. The
# rows file holds every stored script, so the template quotes both programs into the command.
def pre_verify_script(template: str, *, new: str, read_rows: str, check_rows: str) -> str:
    return template.format(
        home=HOME,
        new=new,
        read_rows=shlex.quote(read_rows),
        check_rows=shlex.quote(check_rows),
    )
