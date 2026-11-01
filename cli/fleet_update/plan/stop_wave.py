# meiva:note
# Which hosts a window stops, in which order, and with which flags. Runners stop first and the
# gateway last: a runner keeps working while the gateway it reports to is still up. The
# operator's stop choices reach every host verbatim; `--keep-infra` or `--stop-infra` reaches the
# gateway's stop only.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from .wave_order import stop_order

# meiva:note
# The stop a rebuilding host gets instead of the operator's choices: its extras, shell sessions
# and browser all go, so the helper (a loaded one is never replaced in place) can be rebuilt at
# the next start.
REBUILD_STOP = "--stop-extras --close-shell-sessions --stop-browser --keep-no-services"


@dataclass(frozen=True)
class StopChoices:
    forwarded: tuple[str, ...]
    mode: str
    timeout: int
    infra: str
    message: str
    legacy_message_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StopStep:
    alias: str
    flags: str
    retires_extras: bool


def _flags(choices: StopChoices, *, alias: str, rebuilding: bool, is_gateway: bool) -> str:
    head = (REBUILD_STOP,) if rebuilding else choices.forwarded
    parts = [*head, f"--mode {choices.mode}", f"--timeout {choices.timeout}"]
    if choices.message and alias not in choices.legacy_message_hosts:
        parts.append(choices.message)
    if is_gateway:
        parts.append(choices.infra)
    return " ".join(parts)


# meiva:note
# The stops of one wave: every runner, then the gateway. A rebuilding host retires its extras;
# the gateway keeps its own data-plane choice either way.
def stop_steps(
    *,
    gateway: str,
    runners: tuple[str, ...],
    rebuilding: frozenset[str],
    choices: StopChoices,
) -> tuple[StopStep, ...]:
    return tuple(
        StopStep(
            alias=alias,
            flags=_flags(
                choices,
                alias=alias,
                rebuilding=alias in rebuilding,
                is_gateway=alias == gateway,
            ),
            retires_extras=alias in rebuilding,
        )
        for alias in stop_order(gateway, runners)
    )
