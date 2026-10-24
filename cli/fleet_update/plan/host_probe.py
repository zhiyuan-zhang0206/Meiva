# meiva:note
# What the window learns from one host's probe before anything stops, and what it refuses on:
# a dirty checkout, a post-checkout hook (the guard recurses), or an unfinished release operation.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping

from .journal import parse_journal, stop_completed
from .probe_values import ProbeFlag, probe_flag


# meiva:note
# The `key=value` lines a probe printed, as a mapping. A line without `=` becomes an empty value
# under its own text.
def facts_of(text: str) -> dict[str, str]:
    return {key: value for key, _, value in (line.partition("=") for line in text.splitlines())}


# meiva:note
# Every fact the refusals and the OLD commit read. A probe that did not print one of them did not
# run to its end, which refuses the window instead of crashing it.
_REQUIRED = ("head", "dirty", "hook", "active")


def host_refusals(alias: str, facts: Mapping[str, str]) -> tuple[str, ...]:
    reasons = [f"{alias}: the probe did not report {key}" for key in _REQUIRED if key not in facts]
    if reasons:
        return tuple(reasons)
    hook = probe_flag(f"{alias}.hook", facts["hook"])
    active = probe_flag(f"{alias}.active", facts["active"])
    if facts["dirty"] != "0":
        reasons.append(f"{alias}: $HOME/.meiva/source has uncommitted changes")
    if hook is ProbeFlag.YES:
        reasons.append(f"{alias}: a post-checkout hook (the guard recurses); move it aside")
    if active is ProbeFlag.YES:
        reasons.append(
            f"{alias}: $HOME/.meiva/updates/active names an unfinished release operation"
        )
    return tuple(reasons)


# meiva:note
# Whether the host was serving when the window began. A host is already stopped only when its last
# lifecycle operation is a completed, successful stop and its meiva-root is not running; any other
# host was up, and a failed window brings it back.
def was_serving(facts: Mapping[str, str]) -> bool:
    stopped = facts.get("root") == "down" and stop_completed(parse_journal(facts.get("op", "")))
    return not stopped
