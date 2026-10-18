# meiva:note
# Which capability an agent-facing SDK entry needs. The vocabulary lives in
# `base/agents/impersonation/capabilities.py`; this module owns the SDK side of it:
# the map from a dotted entry (`files.write`) to the one capability it requires. The
# keys are the ones the SDK usage meter reports, so a warning or a refusal names
# exactly what was called.
#
# The map is closed. `capability_for` returns None for an entry nobody classified,
# and the attachment refuses (or, in `warn`, records) that call. A coverage test
# walks the installed surface with `meiva.sdk_surface.metering.instrument_targets`
# and fails when an entry has no classification, so adding an SDK entry forces the
# question at review time instead of after the fact.
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

_READ = Capability.READ
_FILES_WRITE = Capability.FILES_WRITE
_SHELL = Capability.SHELL
_SPAWN = Capability.AGENTS_SPAWN
_CONTROL = Capability.AGENTS_CONTROL
_MESSAGE = Capability.AGENTS_MESSAGE
_SELF = Capability.SELF_LIFECYCLE
_TASKS = Capability.TASKS_WRITE
_UI = Capability.UI
_SCHEDULE = Capability.SCHEDULE

# meiva:note
# One capability per entry. Observation is read; changing another agent's lifecycle
# (`agents.control`, `compaction.submit`) is the sharp one; `self.*` is the borrowed
# identity's own lifecycle and persistent settings; `schedule` outlives the lease.
ENTRY_CAPABILITIES: Mapping[str, Capability] = {
    "help": _READ,
    "understand": _READ,
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

PREFIX_CAPABILITIES: tuple[tuple[str, Capability], ...] = (("mcps.", _SHELL),)


def capability_for(entry: str) -> Capability | None:
    exact = ENTRY_CAPABILITIES.get(entry)
    if exact is not None:
        return exact
    for prefix, capability in PREFIX_CAPABILITIES:
        if entry.startswith(prefix):
            return capability
    return None
