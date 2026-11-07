# meiva:note
# The operation identity a fleet window mints once and stamps on every command it sends
# (task #6024). It answers three questions the 2026-11-02 window left open: *which* update was
# this (`operation_id`, unique per window), *who* started it (`initiator`), and whether the
# release gate's conditions held or were bypassed (`gate` + `gate_reason`).
#
# The receiving side already reads these names: `base/native_process/root_control/client.py`
# builds the caller block from `MEIVA_OPERATION_ID` and the trio below, and every lifecycle
# command's audit row carries them (`cli/database/lifecycle_audit.py`). This module only mints
# and spells them.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal

ID_ENV = "MEIVA_OPERATION_ID"
INITIATOR_ENV = "MEIVA_OPERATION_INITIATOR"
GATE_ENV = "MEIVA_OPERATION_GATE"
GATE_REASON_ENV = "MEIVA_OPERATION_GATE_REASON"

Gate = Literal["satisfied", "overridden"]

# meiva:note
# The target modes that verify a `meiva-release/v1` certificate before anything stops: the
# channel pointer, an explicit CalVer release, and a commit only when a certificate covers it.
# `track-branch` is the one mode that deliberately ships an unreleased branch, so it is the one
# that records `overridden` — the bypass becomes a queryable fact instead of an assumption.
GATED_MODES = frozenset({"channel", "release", "commit"})


@dataclass(frozen=True)
class OperationIdentity:
    operation_id: str
    initiator: str
    gate: Gate
    gate_reason: str


# meiva:note
# The gate verdict of one target request, as data: which mode, and the branch when the mode is
# `track-branch`. A gated mode names itself as the reason a reader can check
# ("channel", "release", "commit"); the bypass names its branch ("track-branch:preview").
def gate_for(target_mode: str, *, branch: str | None = None) -> tuple[Gate, str]:
    if target_mode in GATED_MODES:
        return "satisfied", target_mode
    return "overridden", f"{target_mode}:{branch}" if branch else target_mode


# meiva:note
# The shell prologue every host command carries, as assignments the host's own `meiva` reads
# back into its caller block. Quoted with `shlex.quote` because an initiator or a branch name
# reaches this string from a command line.
def shell_exports(op: OperationIdentity) -> str:
    pairs = (
        (ID_ENV, op.operation_id),
        (INITIATOR_ENV, op.initiator),
        (GATE_ENV, op.gate),
        (GATE_REASON_ENV, op.gate_reason),
    )
    return "export " + " ".join(f"{name}={shlex.quote(value)}" for name, value in pairs) + ";"
