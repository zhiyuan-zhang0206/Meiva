# meiva:note
# The `meiva cluster status` roster table: what each column says about a machine.
#
# Values in, lines out. The shell fetches and decodes `/api/cluster/roster`; this module
# decides the wording and the alignment and prints nothing.

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from base.api_contracts.status import MachineStatus
from base.cluster.machine import format_capabilities

__all__ = ["code_cell", "roster_lines", "schema_mismatch_banner", "status_cell"]

# meiva:note
# The widest label `format_capabilities` emits is "gateway + agent-runner + observability-station"
# (44 characters).
_ROLE_COL_W = 44


# meiva:note
# The roster as aligned text lines: the schema banner above the table, then header,
# separator and one row per machine. Assumes a non-empty roster (the shell short-circuits the
# empty case).
#
# The wire schema carries three capability flags and no single `role` field, so the role
# column is derived through `format_capabilities`. A staging host stays roster-visible but is
# not a fan-out target, so its name carries the mark.
def roster_lines(roster: Sequence[MachineStatus]) -> tuple[str, ...]:
    name_w = max(
        *(len(f"{m.name} (staging)") if m.is_staging else len(m.name) for m in roster),
        len("name"),
    )
    lines = [
        *schema_mismatch_banner(roster),
        f"{'name'.ljust(name_w)}  {'role':<{_ROLE_COL_W}} {'paused':<7} {'status':<10} "
        f"{'code':<10} up since",
        "-" * (name_w + 71),
    ]
    for m in roster:
        status = status_cell(
            online=m.online, identity_mismatch=m.identity_mismatch, stopped_at=m.stopped_at
        )
        display_name = f"{m.name} (staging)" if m.is_staging else m.name
        paused_str = "?" if m.paused is None else "yes" if m.paused else "no"
        up_since = str(m.up_since_at)[:19] if m.online else "—"
        role = format_capabilities(
            m.serve_gateway, m.serve_agent_runner, m.serve_observability_station
        )
        code_str = code_cell(m.running_sha, m.head_sha)
        lines.append(
            f"{display_name.ljust(name_w)}  {role:<{_ROLE_COL_W}} {paused_str:<7} {status:<10} "
            f"{code_str:<10} {up_since}"
        )
    return tuple(lines)


# meiva:note
# Schema disagreement or unavailable evidence is shown even while ops is online.
def schema_mismatch_banner(roster: Sequence[MachineStatus]) -> tuple[str, ...]:
    return tuple(
        f"⚠ schema check on {m.schema_mismatch.machine}: {m.schema_mismatch.kind}; "
        f"{m.schema_mismatch.detail}"
        for m in roster
        if m.schema_mismatch is not None
    )


# meiva:note
# The roster `status` column. `MISMATCH` is a loud third state that outranks online/offline:
# the probe reached an ops server that answered under the WRONG machine_name, so this row's
# gateway_url points at the wrong host, never a green "online".
#
# `STALE-STOP` is the fourth: the probe answered AND the row carries a stop marker, i.e. the
# two sources of truth disagree about whether this host exists. A plain "online" there is
# what hid the 2026-07-28 runner exclusion: the roster read healthy while the update fan-out,
# which filters on that same marker, silently dropped the host. The marker is the wrong one (a
# live probe outranks a latch nothing but `meiva start` clears), so the cell names the
# contradiction rather than picking a side quietly.
def status_cell(*, online: bool, identity_mismatch: bool, stopped_at: datetime | None) -> str:
    if identity_mismatch:
        return "MISMATCH"
    if online:
        return "STALE-STOP" if stopped_at else "online"
    return "stopped" if stopped_at else "offline"


# meiva:note
# One cell for the roster `code` column: the commit the live process is actually running
# (`running_sha`), short. `⚠` when it differs from the node's checkout HEAD (`head_sha`): the
# checkout advanced but the process was not restarted, so it is still running stale code (the
# 2026-07-18 lesson: the checkout alone proved nothing; up-since exposed the old process).
# `—` when the answering process froze no commit: it came up outside the supervised start
# path, or its tree is not a git checkout.
def code_cell(running_sha: str | None, head_sha: str | None) -> str:
    if running_sha is None:
        return "—"
    short = running_sha[:7]
    if head_sha is not None and running_sha != head_sha:
        return f"⚠ {short}"
    return short
