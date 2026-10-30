# meiva:note
# `meiva cluster` machine verbs: thin clients of the gateway's `/api/cluster/*` endpoints, and
# the two `db-authority` verbs that seal and install a remote unit's database capability.
#
# Every machine verb takes the `GatewayAccess` its composition root built from the command's
# own configuration; the wording of each reply lives in `plans.gateway_replies` and the roster
# table in `plans.roster_table`, so this shell only dials, decodes and prints. The
# `db-authority` verbs take the invocation's `CommandRuntime` (the gateway home, the
# configuration and the cluster secret) or the home and transport key their root read, and
# their wording lives in `plans.db_authority_replies`.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import httpx

from base.api_contracts.status import MachineStatus
from base.cluster.machine import (
    GatewayAccess,
    GatewayApiBaseMissing,
    gateway_api_base,
    gateway_auth_headers,
)
from base.config.service_read import ConfigAuthority
from base.host.db_authority.model import AuthorityRefusedError
from base.host.db_authority.unit import UnitIdentity, issue_bundle, write_bundle
from base.host.env.bootstrap import BootstrapFetchError, config_source_is_local
from base.host.env.dotenv_file import env_file_path, read_env_aliases
from base.host.net.http_dial import get as dial_get
from base.host.net.http_dial import post as dial_post
from base.paths.expand import expand_home
from cli.commands.cluster.plans.db_authority_replies import (
    IssuedReport,
    failure_line,
    install_refusal,
    issue_refusal,
    issued_text,
)
from cli.commands.cluster.plans.gateway_replies import (
    NoGatewayUrl,
    PauseReply,
    ReadTimedOut,
    RefusedWithStatus,
    Unreachable,
    not_registered_line,
    pause_lines,
    resume_lines,
    roster_failure_line,
    staging_line,
)
from cli.commands.cluster.plans.roster_table import roster_lines
from cli.commands.cluster.unit_join import join_gateway
from cli.database.runtime import CommandRuntime
from cli.start_identity import require_initialized, stored_values

_MACHINE_VERB_TIMEOUT_S = 8.0
_PAUSE_TIMEOUT_S = 60.0
_STATUS_READ_TIMEOUT_S = 10.0

# meiva:note
# `meiva cluster status` reads the *fresh* roster, whose server-side fan-out probes every
# agent-runner in parallel, each bounded by the gateway's `status_probe_timeout_seconds`. A
# black-holed machine rides that probe to its full bound before the gateway can answer
# (measured 8.12s of wall against the 8.0s default), so a client read budget pinned at the
# probe budget alone collides with it exactly when a machine is down (#4900). This margin
# covers transport/serialization overhead and load jitter; with the default budget a silent
# gateway still reports within 12s.
ROSTER_READ_MARGIN_S = 4.0


# meiva:note
# `meiva cluster mark-staging NAME` / `unmark-staging NAME`.
#
# POSTs /api/cluster/machines/{name}/staging on the gateway, which flips the operator staging
# flag on the machines row. A staging host stays registered and roster-visible but is
# excluded from the agent-runner target set (`list_agent_runners`: the heartbeat probe and
# cluster fan-outs). Exit 1 when the gateway reports no such machine.
def cmd_cluster_mark_staging(name: str, *, is_staging: bool, access: GatewayAccess) -> int:
    url = f"{gateway_api_base(access.gateway_url)}/api/cluster/machines/{name}/staging"
    resp = dial_post(
        url,
        timeout=_MACHINE_VERB_TIMEOUT_S,
        headers=gateway_auth_headers(access),
        json={"is_staging": is_staging},
    )
    if resp.status_code == 404:
        print(not_registered_line(name), file=sys.stderr)
        return 1
    resp.raise_for_status()
    print(staging_line(name, is_staging=is_staging))
    return 0


# meiva:note
# `meiva cluster pause NAME [--reason ...]`: temporarily pull a machine out of the cluster.
#
# POSTs /api/cluster/machines/{name}/pause on the gateway, which drains (reassigns
# in_progress tasks of the machine's agents to the drain owner #405, with a note on each),
# terminates every live agent on the machine and sets the pause latch. From then on the
# machine is hidden from the roster / cluster panel / `meiva.agents.list_machines()`, is not
# probed (no offline signal), is skipped by cluster fan-outs and refuses spawns, so the
# cluster shows only its active members. The registration row (URL/role) is preserved for
# resume. Exit 1 when the gateway reports no such machine or refuses (own gateway).
def cmd_cluster_pause(name: str, *, reason: str | None, access: GatewayAccess) -> int:
    url = f"{gateway_api_base(access.gateway_url)}/api/cluster/machines/{name}/pause"
    resp = dial_post(
        url,
        timeout=_PAUSE_TIMEOUT_S,
        headers=gateway_auth_headers(access),
        json={"reason": reason or ""},
    )
    if resp.status_code == 404:
        print(not_registered_line(name), file=sys.stderr)
        return 1
    if resp.status_code == 400:
        print(resp.json().get("detail", "pause refused"), file=sys.stderr)
        return 1
    resp.raise_for_status()
    body = resp.json()
    reply = PauseReply(
        paused_at=str(body["paused_at"]),
        pause_reason=str(body.get("pause_reason") or ""),
        terminated_agents=int(body["terminated_agents"]),
        force_marked_agents=int(body["force_marked_agents"]),
        reassigned_tasks=int(body["reassigned_tasks"]),
    )
    for line in pause_lines(name, reply):
        print(line)
    return 0


# meiva:note
# `meiva cluster resume NAME`: restore a paused machine as a normal cluster member.
#
# POSTs /api/cluster/machines/{name}/resume on the gateway, which clears the pause latch;
# probing, the roster, cluster fan-outs and spawn acceptance resume immediately. Exit 1 when
# the gateway reports no such machine. Prints the ops checklist for the machine's own side.
def cmd_cluster_resume(name: str, *, access: GatewayAccess) -> int:
    url = f"{gateway_api_base(access.gateway_url)}/api/cluster/machines/{name}/resume"
    resp = dial_post(
        url, timeout=_MACHINE_VERB_TIMEOUT_S, headers=gateway_auth_headers(access), json={}
    )
    if resp.status_code == 404:
        print(not_registered_line(name), file=sys.stderr)
        return 1
    resp.raise_for_status()
    for line in resume_lines(name, resumed=bool(resp.json()["resumed"])):
        print(line)
    return 0


# meiva:note
# `meiva cluster status`: print the full multi-machine roster.
#
# GETs `/api/cluster/roster` on the gateway, which assembles the roster server-side (its own
# row locally plus each agent-runner probed in parallel via the status_probe op). Fails fast
# on any HTTP error rather than masking an unreachable gateway; the transport failures get
# one-line stderr verdicts and a nonzero exit instead of an unhandled traceback, because the
# unreachable-machine case is the exact situation an operator runs this command for.
#
# The read budget is derived, not fixed (#4900): a black-holed machine makes the gateway's
# fresh read ride that machine's probe up to its `status_probe_timeout_seconds` bound, so the
# client budget is that bound plus `read_margin_s`. A fixed budget set to the probe budget
# alone collides with it exactly when a machine is down (the #219 incident shape).
def cmd_cluster_status(
    *,
    access: GatewayAccess,
    status_probe_timeout_s: float,
    read_margin_s: float = ROSTER_READ_MARGIN_S,
) -> int:
    try:
        # meiva:note
        # An operator command: dial every runner now rather than render the heartbeat pass's
        # last probe, which can be a minute old.
        url = f"{gateway_api_base(access.gateway_url)}/api/cluster/roster?fresh=true"
    except GatewayApiBaseMissing as exc:
        print(roster_failure_line(NoGatewayUrl(str(exc))), file=sys.stderr)
        return 1
    budget_s = status_probe_timeout_s + read_margin_s
    try:
        resp = dial_get(url, timeout=budget_s, headers=gateway_auth_headers(access))
    except httpx.TimeoutException as exc:
        failure = ReadTimedOut(url, budget_s, read_margin_s, str(exc))
        print(roster_failure_line(failure), file=sys.stderr)
        return 1
    except httpx.TransportError as exc:
        print(roster_failure_line(Unreachable(url, str(exc))), file=sys.stderr)
        return 1
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            roster_failure_line(RefusedWithStatus(url, resp.status_code, str(exc))),
            file=sys.stderr,
        )
        return 1
    roster = [MachineStatus.model_validate(m) for m in resp.json()]
    if not roster:
        print("(machines table empty — no host has run `meiva start` yet)")
        return 0
    for line in roster_lines(roster):
        print(line)
    return 0


# meiva:note
# GET `/api/cluster/status` and return the decoded ClusterStatus body.
#
# Used by `meiva status`'s gateway-view supplement. Presents the gateway credential (the
# endpoint is authenticated, like every `/api/cluster/*`) so a healthy-but-authed gateway
# reads as up instead of a false `401 unreachable`. Fails fast (`raise_for_status()`) on any
# HTTP error rather than masking an unreachable gateway.
def fetch_gateway_cluster_status(access: GatewayAccess) -> dict[str, object]:
    url = f"{gateway_api_base(access.gateway_url)}/api/cluster/status"
    resp = dial_get(url, timeout=_STATUS_READ_TIMEOUT_S, headers=gateway_auth_headers(access))
    resp.raise_for_status()
    return resp.json()


# meiva:note
# This gateway home when `issue-unit` may run on it, or None after printing the refusal.
#
# Whether the home serves the gateway comes from the environment this invocation was booted
# with and the home's own `.env`; whether its data plane is remote comes from the invocation's
# configuration.
def _issuing_home(command: CommandRuntime) -> Path | None:
    config = command.config
    env_file = read_env_aliases(env_file_path(command.home))
    refusal = issue_refusal(
        serves_gateway=config_source_is_local(config.process_environment(), env_file=env_file),
        remote_plane=config.view.data_plane.is_remote,
    )
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return None
    return command.home.resolve()


# meiva:note
# `meiva cluster db-authority issue-unit`: seal one remote unit's database capability.
#
# Runs on the gateway home. The bundle carries the ACTIVE write generation's runner login and
# (while the API is authenticated) its API admission (the runner API token, the gateway
# token's digest and the telemetry token), the endpoint bootstrap serves, bound to
# (`machine`, `home`) and expiring after `ttl_hours`. It is written 0600 to `out` (never
# overwritten) and sealed under a transport key printed once here; the unit installs it with
# `meiva cluster db-authority install-unit <bundle>` (a first join passes it to
# `meiva init --db-capability`) and that key in MEIVA_DB_CAPABILITY_KEY. Refused on a pure
# agent-runner, a remote-managed plane and a home without an active generation. `home` is the
# unit's home on its own machine; the gateway home, the configuration and the cluster secret
# are the invocation's (`command`).
def cmd_db_authority_issue_unit(
    *, machine: str, home: str, out: str, ttl_hours: float, command: CommandRuntime
) -> int:
    # meiva:note
    # `--out` is the operator's own path: a leading `~` is the home this invocation read, and a
    # `~someone/...` spelling names another account's home, which this command does not read.
    try:
        target = expand_home(out, user_home=command.session.user_home).absolute()
    except ValueError as exc:
        print(failure_line("issue-unit", str(exc)), file=sys.stderr)
        return 1
    gateway_home = _issuing_home(command)
    if gateway_home is None:
        return 1
    config = command.config
    try:
        unit = UnitIdentity(machine=machine, home=home)
        issued = issue_bundle(
            gateway_home,
            unit=unit,
            endpoint=ConfigAuthority.deferred(
                runtime=config.view,
                build_all_domains=config.complete_read_model,
                env_path=gateway_home / ".env",
            ).served_db_endpoint(),
            cluster_secret=config.view.data_plane.cluster_secret,
            ttl_s=ttl_hours * 3600,
            now=command.clock.now().timestamp(),
        )
        write_bundle(target, issued.envelope)
    except (AuthorityRefusedError, ValueError, RuntimeError, OSError) as exc:
        print(failure_line("issue-unit", str(exc)), file=sys.stderr)
        return 1
    expires = datetime.fromtimestamp(issued.expires_at).astimezone().isoformat(timespec="seconds")
    report = IssuedReport(
        unit=unit.describe(),
        generation=issued.generation,
        expires=expires,
        target=str(target),
        transport_key=issued.transport_key,
    )
    print(issued_text(report))
    return 0


# meiva:note
# `meiva cluster db-authority install-unit`: install a sealed capability on this unit.
#
# Runs on an initialized agent-runner home, with the bundle's transport key (the composition
# root takes it out of MEIVA_DB_CAPABILITY_KEY). The bundle must name this unit (machine and
# home), the endpoint the gateway serves now and a generation not older than the installed
# one; the unit's gateway answers the bundle's own API token before anything is written, and
# the bundle file is deleted once installed. After a write-generation rotation: stop the unit,
# install the new bundle, start it. Refused on a gateway home (it keeps its own ledger) and on
# a home `meiva init` has not initialized.
def cmd_db_authority_install_unit(
    *, bundle: str, transport_key: str, home: Path, now: float
) -> int:
    try:
        admitted = require_initialized(home)
    except (RuntimeError, ValueError, OSError) as exc:
        print(failure_line("install-unit", str(exc)), file=sys.stderr)
        return 1
    refusal = install_refusal(admitted.roles)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1
    try:
        join_gateway(stored_values(home), home, bundle, transport_key, now=now)
    except (AuthorityRefusedError, BootstrapFetchError, ValueError, RuntimeError, OSError) as exc:
        print(failure_line("install-unit", str(exc)), file=sys.stderr)
        return 1
    return 0
