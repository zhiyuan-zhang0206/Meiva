# meiva:note
# `meiva schedules` — manage gateway-supervised schedules (thin client over the gateway).
#
# A schedule is a persistent session (a `script` + the `command` that runs it) the
# gateway's ScheduleManager keeps alive in a pty session. This is the
# shell-level management surface: list / get / create / update / delete plus the control verbs
# (start / stop / restart), the two read-only observation verbs (logs / runs), and the local
# `verify` dry-import sweep.
#
# Most verbs forward to an existing route under `/api/schedules` and render the
# result — the gateway owns the effect (script syntax check, version snapshot,
# session reconcile), the CLI adds only arg parsing + rendering. This is the
# operator/agent primitive form: `POST /api/keyed/v1/schedules/draft` (hand a natural-
# language request to a schedule_writer agent) is deliberately NOT exposed here —
# the CLI writes a script it already has, it does not run a conversation. Two
# verbs are local: `provision` (restores the built-ins; never touches the gateway, so it
# works while the gateway is down) and `verify` (reads the in-store scripts through the
# gateway, then dry-imports them against this checkout).
#
# Every verb that names a schedule accepts a name or a numeric id.

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, TypeAdapter

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import GatewayAccess
from cli.boundary.json_body import response_object
from cli.boundary.options import options
from cli.database import database_factory_of
from cli.database.runtime import runtime_of

_TIMEOUT_S = 15.0


# meiva:note
# One schedule as the gateway lists it, with the fields the CLI prints. `script` is present only
# on the single-schedule read. Other fields of the answer are ignored.
class _Schedule(BaseModel):
    id: int
    name: str
    command: str | None = None
    enabled: bool
    status: str
    description: str | None = None
    machine: str | None = None
    owner_plugin: str | None = None
    placement_status: str | None = None
    placement_reason: str | None = None
    last_error: str | None = None
    script: str | None = None


# meiva:note
# One row of `GET /api/schedules/{id}/runs`.
class _Run(BaseModel):
    ran_at: str
    ok: bool | None
    agent_id: int | None
    note: str | None


_SCHEDULES = TypeAdapter(list[_Schedule])
_RUNS = TypeAdapter(list[_Run])


# meiva:note
# The answer of `GET /api/schedules/{id}/logs`.
class _LogsView(BaseModel):
    source: str
    lines: list[str] = []


def _schedule(resp: httpx.Response) -> _Schedule:
    return _Schedule.model_validate_json(resp.content)


def _schedules(resp: httpx.Response) -> list[_Schedule]:
    return _SCHEDULES.validate_json(resp.content)


def _gateway_base(access: GatewayAccess) -> str:
    from base.cluster.machine import gateway_api_base

    return gateway_api_base(access.gateway_url)


def _headers(access: GatewayAccess) -> dict[str, str]:
    from base.cluster.machine import gateway_auth_headers

    return gateway_auth_headers(access)


def _mutation_headers(access: GatewayAccess, idempotency_key: str) -> dict[str, str]:
    from base.api_contracts.idempotency import keyed_headers

    return {**_headers(access), **keyed_headers(idempotency_key)}


# ── list ──


def cmd_schedules_ls(access: GatewayAccess) -> int:
    from base.host.net.http_dial import get as dial_get

    resp = dial_get(
        f"{_gateway_base(access)}/api/schedules", timeout=_TIMEOUT_S, headers=_headers(access)
    )
    resp.raise_for_status()
    rows = _schedules(resp)

    if not rows:
        print("(no schedules)")
        return 0

    id_w = max(len("id"), *(len(str(r.id)) for r in rows))
    status_w = max(len("status"), *(len(r.status) for r in rows))
    machine_w = max(len("machine"), *(len(r.machine or "") for r in rows))
    placement_w = max(len("placement"), *(len(r.placement_status or "") for r in rows))
    print(
        f"{'id'.rjust(id_w)}  {'on':<3}  {'status'.ljust(status_w)}  "
        f"{'machine'.ljust(machine_w)}  {'placement'.ljust(placement_w)}  name"
    )
    for r in rows:
        on = "yes" if r.enabled else "no"
        m_str = r.machine or ""
        p_str = r.placement_status or ""
        print(
            f"{str(r.id).rjust(id_w)}  {on:<3}  {r.status.ljust(status_w)}  "
            f"{m_str.ljust(machine_w)}  {p_str.ljust(placement_w)}  {r.name}"
        )
    return 0


# ── get ──


def cmd_schedules_get(access: GatewayAccess, identifier: str) -> int:
    """`meiva schedules get <name-or-id>` — the full schedule including its script."""
    row = _fetch(access, identifier)
    if row is None:
        return 1
    _print_schedule(row, with_script=True)
    return 0


# ── create ──


def cmd_schedules_create(
    access: GatewayAccess,
    name: str,
    script: str | None,
    script_file: str | None,
    command: str | None,
    description: str | None,
    *,
    machine: str | None = None,
    disabled: bool = False,
    idempotency_key: str,
) -> int:
    """`meiva schedules create --name N (--script S | --script-file F) [...]`.

    Exactly one of --script / --script-file supplies the body (`--script-file -`
    reads stdin, the natural form for a heredoc). The gateway `compile()`-checks
    the script and rejects a SyntaxError with 400, so a broken script never
    reaches the runner. Created enabled unless --disabled; an enabled schedule is
    launched by the reconcile loop within a poll interval."""
    from base.host.net.http_dial import post as dial_post

    source = _read_script(script, script_file)
    if source is None:
        return 1

    body: dict[str, object] = {"name": name, "script": source, "enabled": not disabled}
    if command is not None:
        body["command"] = command
    if description is not None:
        body["description"] = description
    if machine is not None:
        body["machine"] = machine

    resp = dial_post(
        f"{_gateway_base(access)}/api/schedules",
        json=body,
        timeout=_TIMEOUT_S,
        headers=_mutation_headers(access, idempotency_key),
    )
    if resp.status_code == 409:
        print(f"schedule named {name!r} already exists", file=sys.stderr)
        return 1
    if resp.status_code == 400:
        print(_detail(resp), file=sys.stderr)
        return 1
    _die_on_unexpected_error(resp)
    row = _schedule(resp)
    print(f"  ✓ created schedule #{row.id} ({row.name})")
    _print_schedule(row, with_script=False)
    return 0


# ── update ──


def cmd_schedules_update(
    access: GatewayAccess,
    identifier: str,
    name: str | None,
    script: str | None,
    script_file: str | None,
    command: str | None,
    description: str | None,
    *,
    machine: str | None = None,
    enabled: bool | None,
    idempotency_key: str,
) -> int:
    """`meiva schedules update <name-or-id> [--name N] [--script S | --script-file F] ...`.

    Partial update — only the flags passed change (PUT with an exclude-unset
    body). A script/command change is snapshotted into `schedule_versions` and,
    when the schedule is enabled, the session is relaunched onto the new script
    immediately. --enable / --disable converge when they change desired state:
    the same field `start`/`stop` flip is paired with session reaping or
    creation before the command returns, while a same-value update is a no-op."""
    from base.host.net.http_dial import put as dial_put

    schedule_id = _resolve_id(access, identifier)
    if schedule_id is None:
        return 1

    body: dict[str, object] = {}
    if name is not None:
        body["name"] = name
    if script is not None or script_file is not None:
        source = _read_script(script, script_file)
        if source is None:
            return 1
        body["script"] = source
    if command is not None:
        body["command"] = command
    if description is not None:
        body["description"] = description
    if machine is not None:
        body["machine"] = machine
    if enabled is not None:
        body["enabled"] = enabled

    if not body:
        print(
            "at least one of --name / --script / --script-file / --command / "
            "--description / --machine / --enable / --disable is required",
            file=sys.stderr,
        )
        return 1

    resp = dial_put(
        f"{_gateway_base(access)}/api/schedules/{schedule_id}",
        json=body,
        timeout=_TIMEOUT_S,
        headers=_mutation_headers(access, idempotency_key),
    )
    if resp.status_code in (400, 404, 409):
        print(_detail(resp), file=sys.stderr)
        return 1
    _die_on_unexpected_error(resp)
    row = _schedule(resp)
    print(f"  ✓ updated schedule #{row.id} ({row.name})")
    _print_schedule(row, with_script=False)
    return 0


# ── delete ──


def cmd_schedules_delete(access: GatewayAccess, identifier: str, *, force: bool = False) -> int:
    """`meiva schedules delete <name-or-id> [--force]`.

    Prompts unless --force. The gateway kills the now-orphaned session and
    removes the schedule's work dir; its run history goes with the row."""
    from base.host.net.http_dial import delete as dial_delete

    schedule_id = _resolve_id(access, identifier)
    if schedule_id is None:
        return 1

    if not force:
        answer = input(f"Delete schedule {identifier!r} (id={schedule_id})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0

    resp = dial_delete(
        f"{_gateway_base(access)}/api/schedules/{schedule_id}",
        timeout=_TIMEOUT_S,
        headers=_headers(access),
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    print(f"  ✓ deleted schedule #{schedule_id}")
    return 0


# ── control ──


def _control(access: GatewayAccess, identifier: str, verb: str, idempotency_key: str) -> int:
    """Shared POST for start / stop / restart — same shape, different sub-path."""
    from base.host.net.http_dial import post as dial_post

    schedule_id = _resolve_id(access, identifier)
    if schedule_id is None:
        return 1

    resp = dial_post(
        f"{_gateway_base(access)}/api/schedules/{schedule_id}/{verb}",
        timeout=_TIMEOUT_S,
        headers=_mutation_headers(access, idempotency_key),
    )
    if resp.status_code in (404, 409):
        print(_detail(resp), file=sys.stderr)
        return 1
    _die_on_unexpected_error(resp)
    row = _schedule(resp)
    print(f"  ✓ schedule #{row.id} {verb}: enabled={row.enabled} status={row.status}")
    return 0


def cmd_schedules_start(access: GatewayAccess, identifier: str, *, idempotency_key: str) -> int:
    """`meiva schedules start <name-or-id>` — enable + launch the session now."""
    return _control(access, identifier, "start", idempotency_key)


def cmd_schedules_stop(access: GatewayAccess, identifier: str, *, idempotency_key: str) -> int:
    """`meiva schedules stop <name-or-id>` — disable + kill the session now."""
    return _control(access, identifier, "stop", idempotency_key)


def cmd_schedules_restart(access: GatewayAccess, identifier: str, *, idempotency_key: str) -> int:
    """`meiva schedules restart <name-or-id>` — relaunch on the current script,
    clearing crash backoff. 409 if the schedule is disabled (use `start`)."""
    return _control(access, identifier, "restart", idempotency_key)


# ── observation ──


def cmd_schedules_logs(access: GatewayAccess, identifier: str, lines: int) -> int:
    """`meiva schedules logs <name-or-id> [--lines N]` — recent output.

    Source is the live session capture when a session is up, the session's
    PTY transcript file when it is gone (a finished/crashed runner's output
    survives there), `last_error` (the last crash traceback), or `none`."""
    from base.host.net.http_dial import get as dial_get

    schedule_id = _resolve_id(access, identifier)
    if schedule_id is None:
        return 1

    resp = dial_get(
        f"{_gateway_base(access)}/api/schedules/{schedule_id}/logs",
        params={"lines": lines},
        timeout=_TIMEOUT_S,
        headers=_headers(access),
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    view = _LogsView.model_validate_json(resp.content)
    if view.source == "none":
        print("(no output yet — no live session and no recorded error)")
        return 0
    print(f"── source: {view.source} ──")
    for line in view.lines:
        print(line)
    return 0


def cmd_schedules_runs(access: GatewayAccess, identifier: str, limit: int) -> int:
    """`meiva schedules runs <name-or-id> [--limit N]` — run history, newest first."""
    from base.host.net.http_dial import get as dial_get

    schedule_id = _resolve_id(access, identifier)
    if schedule_id is None:
        return 1

    resp = dial_get(
        f"{_gateway_base(access)}/api/schedules/{schedule_id}/runs",
        params={"limit": limit},
        timeout=_TIMEOUT_S,
        headers=_headers(access),
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    rows = _RUNS.validate_json(resp.content)
    if not rows:
        print("(no runs yet)")
        return 0
    print(f"{'ran_at':<28}  {'ok':<5}  {'agent':<6}  note")
    for r in rows:
        ok = "-" if r.ok is None else ("yes" if r.ok else "no")
        agent = "-" if r.agent_id is None else str(r.agent_id)
        head, *rest = (r.note or "").splitlines() or [""]
        print(f"{r.ran_at:<28}  {ok:<5}  {agent:<6}  {head}")
        for line in rest:  # a crashed run's traceback follows its first line
            print(f"    {line}")
    return 0


# ── helpers ──


def cmd_schedules_provision(
    *,
    database_factory: DatabaseFactory,
    machine: str = "gateway",
    home: Path | None = None,
) -> int:
    """`meiva schedules provision` — bring the built-in schedules in line with this
    checkout's schedules/manifest.json: create the missing ones (product schedules —
    self-evolution, memory — enabled; cluster-operator schedules, none today,
    would be present but disabled) and resync the `script` / `command` of
    existing ones whose DB snapshot differs from the repo template (a live session is
    relaunched onto it by the schedule-manager; a stopped one picks it up when started).

    Unlike the other `meiva schedules` verbs this writes the DB directly instead
    of going through the gateway API: it is an initialization/restore action
    that must also work while the gateway is down (and the schedule-manager runs
    the same provision at every start). Idempotent — `enabled` and `description` of an
    existing schedule are never modified, and a schedule that is not in the manifest
    (an agent-created one) is never read."""
    from base.daemon.schedules.builtin_schedules import provision_builtin_schedules
    from base.daemon.schedules.plugin_schedules import reconcile_plugin_schedules
    from base.paths import meiva_home

    resolved_home = home if home is not None else meiva_home()
    with database_factory().write_transaction() as conn:
        result = provision_builtin_schedules(conn, machine=machine)
        plugin_result = reconcile_plugin_schedules(conn, home=resolved_home)
    if result.created:
        print(f"provisioned built-in schedules: {', '.join(result.created)}")
    if result.resynced:
        print(f"resynced built-in schedule scripts to this checkout: {', '.join(result.resynced)}")
    if not result.created and not result.resynced:
        print("(all built-in schedules already present and current)")
    if plugin_result.created:
        print(f"provisioned plugin schedules: {', '.join(plugin_result.created)}")
    if plugin_result.updated:
        print(f"resynced plugin schedules: {', '.join(plugin_result.updated)}")
    if plugin_result.removed:
        print(f"removed orphaned plugin schedules: {', '.join(plugin_result.removed)}")
    return 0


def _read_script(script: str | None, script_file: str | None) -> str | None:
    """Resolve the script body from --script / --script-file (`-` = stdin).

    Exactly one source must be given; returns None + prints to stderr otherwise
    (or if the file is unreadable)."""
    if (script is None) == (script_file is None):
        print("exactly one of --script / --script-file is required", file=sys.stderr)
        return None
    if script is not None:
        return script
    assert script_file is not None  # noqa: S101 — narrowed by the XOR check above
    if script_file == "-":
        return sys.stdin.read()
    try:
        return Path(script_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"cannot read script file: {e}", file=sys.stderr)
        return None


def _resolve_id(access: GatewayAccess, identifier: str) -> int | None:
    """Resolve a name-or-id string to a numeric schedule id. Returns None +
    prints to stderr when the name matches nothing."""
    from base.host.net.http_dial import get as dial_get

    with contextlib.suppress(ValueError):
        return int(identifier)

    resp = dial_get(
        f"{_gateway_base(access)}/api/schedules", timeout=_TIMEOUT_S, headers=_headers(access)
    )
    resp.raise_for_status()
    match = next((r for r in _schedules(resp) if r.name == identifier), None)
    if match is None:
        print(f"schedule named {identifier!r} not found", file=sys.stderr)
        return None
    return match.id


def _fetch(access: GatewayAccess, identifier: str) -> _Schedule | None:
    """GET one full schedule (with script) by name or id."""
    from base.host.net.http_dial import get as dial_get

    schedule_id = _resolve_id(access, identifier)
    if schedule_id is None:
        return None
    resp = dial_get(
        f"{_gateway_base(access)}/api/schedules/{schedule_id}",
        timeout=_TIMEOUT_S,
        headers=_headers(access),
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return None
    resp.raise_for_status()
    return _schedule(resp)


def _detail(resp: httpx.Response) -> str:
    """The gateway's `detail` string, falling back to the raw body — the
    actionable part of a 400/404/409 (syntax error line, name clash, ...)."""
    with contextlib.suppress(ValueError, TypeError):
        payload = response_object(resp)
        if "detail" in payload:
            return str(payload["detail"])
    return str(resp.text)


def _die_on_unexpected_error(resp: httpx.Response) -> None:
    """Print the body of an error the caller did not classify, then raise.

    The verbs handle the statuses the router documents (400/404/409); anything
    else — a 422 body-validation failure, a 5xx — would otherwise surface as a
    bare httpx traceback with the response body swallowed. Printing first keeps
    the gateway's reason visible while still failing loudly."""
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()


def _print_schedule(row: _Schedule, *, with_script: bool) -> None:
    """Pretty-print a schedule row. `with_script` includes the (potentially
    large) script body — on for `get`, off for the create/update echo."""
    print(f"  id:          {row.id}")
    print(f"  name:        {row.name}")
    if row.description:
        print(f"  description: {row.description}")
    print(f"  command:     {row.command}")
    print(f"  enabled:     {row.enabled}")
    print(f"  status:      {row.status}")
    if row.machine:
        print(f"  machine:     {row.machine}")
    if row.owner_plugin:
        print(f"  owner_plugin: {row.owner_plugin}")
    if row.placement_status:
        p_str = row.placement_status
        if row.placement_reason:
            p_str += f" ({row.placement_reason})"
        print(f"  placement:   {p_str}")
    if row.last_error:
        print(f"  last_error:  {row.last_error.splitlines()[-1]}")
    if with_script:
        print("  script:")
        for line in (row.script or "").splitlines():
            print(f"    {line}")


# ── argparse handlers (wired by the cli/parsers tree) ──


@dataclass(frozen=True)
class _Identifier:
    identifier: str


@dataclass(frozen=True)
class _CreateOptions:
    name: str
    script: str | None
    script_file: str | None
    command: str | None
    description: str | None
    machine: str | None
    disabled: bool


@dataclass(frozen=True)
class _UpdateOptions:
    identifier: str
    name: str | None
    script: str | None
    script_file: str | None
    command: str | None
    description: str | None
    machine: str | None
    enable: bool
    disable: bool


@dataclass(frozen=True)
class _DeleteOptions:
    identifier: str
    force: bool


@dataclass(frozen=True)
class _LogsOptions:
    identifier: str
    lines: int


@dataclass(frozen=True)
class _RunsOptions:
    identifier: str
    limit: int


@dataclass(frozen=True)
class _ProvisionOptions:
    machine: str | None


def _access(args: argparse.Namespace) -> GatewayAccess:
    from base.cluster.machine.boot import gateway_access_from_boot

    return gateway_access_from_boot(runtime_of(args).config)


def h_schedules_ls(args: argparse.Namespace) -> int:
    return cmd_schedules_ls(_access(args))


def h_schedules_get(args: argparse.Namespace) -> int:
    return cmd_schedules_get(_access(args), options(args, _Identifier).identifier)


def h_schedules_create(args: argparse.Namespace) -> int:
    opts = options(args, _CreateOptions)
    return cmd_schedules_create(
        _access(args),
        opts.name,
        opts.script,
        opts.script_file,
        opts.command,
        opts.description,
        machine=opts.machine,
        disabled=opts.disabled,
        idempotency_key=runtime_of(args).new_id(),
    )


def h_schedules_update(args: argparse.Namespace) -> int:
    opts = options(args, _UpdateOptions)
    # --enable / --disable are one tri-state: None = leave the flag alone.
    enabled = True if opts.enable else (False if opts.disable else None)
    return cmd_schedules_update(
        _access(args),
        opts.identifier,
        opts.name,
        opts.script,
        opts.script_file,
        opts.command,
        opts.description,
        machine=opts.machine,
        enabled=enabled,
        idempotency_key=runtime_of(args).new_id(),
    )


def h_schedules_delete(args: argparse.Namespace) -> int:
    opts = options(args, _DeleteOptions)
    return cmd_schedules_delete(_access(args), opts.identifier, force=opts.force)


def h_schedules_start(args: argparse.Namespace) -> int:
    return cmd_schedules_start(
        _access(args),
        options(args, _Identifier).identifier,
        idempotency_key=runtime_of(args).new_id(),
    )


def h_schedules_stop(args: argparse.Namespace) -> int:
    return cmd_schedules_stop(
        _access(args),
        options(args, _Identifier).identifier,
        idempotency_key=runtime_of(args).new_id(),
    )


def h_schedules_restart(args: argparse.Namespace) -> int:
    return cmd_schedules_restart(
        _access(args),
        options(args, _Identifier).identifier,
        idempotency_key=runtime_of(args).new_id(),
    )


def h_schedules_logs(args: argparse.Namespace) -> int:
    opts = options(args, _LogsOptions)
    return cmd_schedules_logs(_access(args), opts.identifier, opts.lines)


def h_schedules_runs(args: argparse.Namespace) -> int:
    opts = options(args, _RunsOptions)
    return cmd_schedules_runs(_access(args), opts.identifier, opts.limit)


def h_schedules_provision(args: argparse.Namespace) -> int:
    runtime = runtime_of(args)
    machine = options(args, _ProvisionOptions).machine
    if machine is None:
        machine = runtime.machine.name()
    return cmd_schedules_provision(
        database_factory=database_factory_of(args),
        machine=machine or "gateway",
        home=runtime.home,
    )
