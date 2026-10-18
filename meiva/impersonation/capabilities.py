# meiva:note
# Which capabilities an agent-facing SDK entry needs. The vocabulary lives in
# `base/agents/impersonation/capabilities.py`; this module owns the SDK side of it:
# the map from a dotted entry (`files.write`) to the capabilities it requires. The
# keys are the ones the SDK usage meter reports, so a warning or a refusal names
# exactly what was called.
#
# An entry declares a **tuple**, ordered general -> specific, because one surface can
# need more than one authority: `meiva.understand` reads local files *and* sends their
# content to a model outside the cluster, so a lease must hold `read` as well as
# `external.model`. The last element is the entry's **primary** capability - the one
# that names what the entry is really about - and it is what a refusal records as
# `capability`; the whole tuple is recorded as `capabilities`. Singletons are written
# as one-element tuples, so there is one shape and no special case.
#
# The map is closed. `required_capabilities` returns None for an entry nobody
# classified, and the attachment refuses (or, in `warn`, records) that call. A
# coverage test walks the installed surface with
# `meiva.sdk_surface.metering.instrument_targets` and fails when an entry has no
# classification, so adding an SDK entry forces the question at review time instead
# of after the fact.
#
# Dynamic MCP tools cannot be enumerated: `meiva.mcps.<server>.<tool>` resolves at
# call time, so the namespace walk never sees it. Those calls are metered as
# `mcps.<server>.<tool>` by the call funnel, and the prefix rule below covers them
# with machine authority - a browser signed in as the platform user, or a remote
# account an MCP server writes through, is the same authority as `shell` here.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping

from base.agents.impersonation.capabilities import Capability

_READ = (Capability.READ,)
_FILES_WRITE = (Capability.FILES_WRITE,)
_SHELL = (Capability.SHELL,)
_SPAWN = (Capability.AGENTS_SPAWN,)
_CONTROL = (Capability.AGENTS_CONTROL,)
_MESSAGE = (Capability.AGENTS_MESSAGE,)
_SELF = (Capability.SELF_LIFECYCLE,)
_TASKS = (Capability.TASKS_WRITE,)
_UI = (Capability.UI,)
_SCHEDULE = (Capability.SCHEDULE,)
_UNDERSTAND = (Capability.READ, Capability.EXTERNAL_MODEL)

# meiva:note
# One requirement tuple per entry, general -> specific. Observation is read; changing
# another agent's lifecycle (`agents.control`, `compaction.submit`) is the sharp one;
# `self.*` is the borrowed identity's own lifecycle and persistent settings;
# `schedule` outlives the lease. Three rows are not what their names suggest:
#   * `understand` needs `read` **and** `external.model`: it bills the user, ships
#     local file content to a model outside the cluster, and reads the paths the
#     caller names. Requiring both means a lease granted only one of them cannot
#     exfiltrate files through it, in either direction.
#   * `shell.sessions.capture` is `read` because it only observes; the observation
#     can still contain credentials printed by a previous command, so `read` here
#     means "may watch this machine", not "the output is safe".
#   * `mcps.*` is machine authority via the prefix rule: a signed-in browser or a
#     remote account is the same authority as `shell`, and a tool cannot be
#     enumerated for a finer split.
ENTRY_CAPABILITIES: Mapping[str, tuple[Capability, ...]] = {
    "help": _READ,
    "understand": _UNDERSTAND,
    "cwd.get": _READ,
    "cwd.set": _READ,
    "files.read": _READ,
    "files.glob": _READ,
    "files.write": _FILES_WRITE,
    "files.append": _FILES_WRITE,
    "files.edit": _FILES_WRITE,
    "files.delete": _FILES_WRITE,
    "agents.list_agents": _READ,
    "agents.get_status": _READ,
    "agents.get_neighbors": _READ,
    "agents.get_ancestors": _READ,
    "agents.get_last_message": _READ,
    "agents.get_launch_attempt": _READ,
    "agents.commands": _READ,
    "agents.list_machines": _READ,
    "agents.spawn": _SPAWN,
    "agents.retry_launch": _SPAWN,
    "agents.terminate": _CONTROL,
    "agents.restart": _CONTROL,
    "agents.resurrect": _CONTROL,
    "agents.send_message": _MESSAGE,
    "agents.work.observe": _READ,
    "agents.work.restart_status": _READ,
    "agents.work.cancel": _CONTROL,
    "agents.work.restart": _CONTROL,
    "agents.compaction.observe": _READ,
    "agents.compaction.status": _READ,
    "agents.compaction.submit": _CONTROL,
    "self.restart": _SELF,
    "self.terminate": _SELF,
    "self.compact": _SELF,
    "self.pause_heartbeat": _SELF,
    "self.set_label": _SELF,
    "self.attach": _READ,
    "shell.run": _SHELL,
    "shell.run_background": _SHELL,
    "shell.sessions.new": _SHELL,
    "shell.sessions.send": _SHELL,
    "shell.sessions.send_keys": _SHELL,
    "shell.sessions.kill": _SHELL,
    "shell.sessions.renew": _SHELL,
    "shell.sessions.capture": _READ,
    "shell.sessions.list": _READ,
    "tasks.list": _READ,
    "tasks.get": _READ,
    "tasks.create": _TASKS,
    "tasks.create_and_assign": _TASKS,
    "tasks.update": _TASKS,
    "tasks.log": _TASKS,
    "ui.notify": _UI,
    "ui.edit_notice": _UI,
    "ui.dismiss_notice": _UI,
    "ui.serve": _UI,
    "ui.show": _UI,
    "ui.close": _UI,
    "watcher.launch": _SCHEDULE,
    "watcher.cron": _SCHEDULE,
    "watcher.at": _SCHEDULE,
    "memory.search": _READ,
    "web.search": _READ,
    "web.fetch": _READ,
}

PREFIX_CAPABILITIES: tuple[tuple[str, tuple[Capability, ...]], ...] = (("mcps.", _SHELL),)


# meiva:note
# The requirement tuple for an entry, or None when nobody classified it - the caller
# refuses that entry rather than guessing a requirement.
def required_capabilities(entry: str) -> tuple[Capability, ...] | None:
    exact = ENTRY_CAPABILITIES.get(entry)
    if exact is not None:
        return exact
    for prefix, requirements in PREFIX_CAPABILITIES:
        if entry.startswith(prefix):
            return requirements
    return None


# meiva:note
# The capability an entry is really about: the most specific element of its ordered
# requirement tuple. A refusal records it as `capability` for continuity with events
# written before entries could need more than one, while `capabilities` carries the
# whole set.
def primary_capability(requirements: tuple[Capability, ...]) -> Capability:
    return requirements[-1]
