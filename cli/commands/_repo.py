"""Path / role helpers and preflight checks used by CLI commands.

This module owns the capability read, repo root, and memory-pool / frontend /
schema / gateway / register preflight checks. Service declarations and roster
selection are defined in services.supervision.roster.service_spec, services.supervision.roster, and services.supervision.roster.spec;
callers import those definitions directly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from base.clock import Timers
from base.cluster.machine import GatewayAccess, MachineIdentityReader, MachineRoles
from base.cluster.machine.boot import gateway_access_from_boot
from base.db import Database
from base.deploy.progress_timeout import GATEWAY_PREFLIGHT_BUDGET_S
from base.sessions.env_forwarding import frontend_toolchain_env
from cli.commands._setup import SetupValues
from cli.database.runtime import CommandRuntime


def roles_or_none(*, machine: MachineIdentityReader) -> MachineRoles | None:
    """Read the capability set; on missing or invalid value return None — the
    explicit "this host's role is not resolvable yet" state, distinct from a
    (never-valid) empty capability set. stop/status/converge should not be
    blocked by unfinished setup, so they treat None conservatively."""
    from base.cluster.machine import MachineRoleInvalid, MachineRoleMissing

    try:
        return machine.role()
    except (MachineRoleMissing, MachineRoleInvalid):
        return None


def repo_root() -> Path:
    """cli/main.py -> cli/ -> repo root.

    cli/commands/ is one level deeper than the old cli/commands.py, so
    parents[2] from this file lands at the same repo root.
    """
    return Path(__file__).resolve().parents[2]


_FOREIGN_NODE_MODULES_MARKERS = (".package-lock.json", ".yarn-integrity")
_IMPORTER_DEP_GROUPS = frozenset({"dependencies", "devDependencies"})


def _unquote_yaml_scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _lock_body_lines(lock_text: str) -> list[str] | None:
    lines = [
        line
        for line in lock_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or not lines[0].startswith("lockfileVersion:"):
        return None
    if _unquote_yaml_scalar(lines[0].partition(":")[2]) != "9.0":
        return None
    return lines[1:]


def _yaml_line_entry(line: str) -> tuple[int, str, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    key, sep, rest = line.strip().partition(":")
    if not sep or indent % 2:
        return None
    return indent // 2, _unquote_yaml_scalar(key), rest


def _is_root_importer_version(scope: tuple[str, ...], level: int, key: str) -> bool:
    return (
        level == 4
        and scope[:2] == ("importers", ".")
        and scope[2] in _IMPORTER_DEP_GROUPS
        and key == "version"
    )


def _locked_root_importer_versions(lock_text: str) -> dict[str, str] | None:
    body = _lock_body_lines(lock_text)
    if body is None:
        return None
    scope: tuple[str, ...] = ("", "", "", "")
    saw_root = False
    versions: dict[str, str] = {}
    for line in body:
        parsed = _yaml_line_entry(line)
        if parsed is None:
            continue
        level, clean_key, rest = parsed
        if level == 0 and scope[0] == "importers":
            break
        if level < 4:
            scope = (*scope[:level], clean_key, *("",) * (3 - level))
            saw_root = saw_root or scope[:2] == ("importers", ".")
        elif _is_root_importer_version(scope, level, clean_key):
            version = _unquote_yaml_scalar(rest).partition("(")[0].strip()
            if not version:
                return None
            versions[scope[3]] = version
    return versions if saw_root else None


# meiva:note
# Detect a node_modules that no longer matches the pnpm lockfile.
#
# Called only when the install stamp matches, i.e. `pnpm-lock.yaml` has not changed since the
# last `pnpm install --frozen-lockfile` — the question is what happened to `node_modules`
# afterwards. Two cheap layers:
#
# - Foreign package-manager markers (`.package-lock.json` / `.yarn-integrity`) and required pnpm
#   metadata (`.modules.yaml`). A foreign install touches neither `pnpm-lock.yaml` nor the stamp,
#   so the stamp path would otherwise trust it forever.
# - Installed versions of direct dependencies vs `pnpm-lock.yaml` — catches drift that leaves
#   `.modules.yaml` present.
def _node_modules_drift(fe: Path, node_modules: Path) -> str | None:
    for marker in _FOREIGN_NODE_MODULES_MARKERS:
        if (node_modules / marker).exists():
            return f"foreign package-manager marker {marker!r}"
    if not (node_modules / ".modules.yaml").is_file():
        return "missing pnpm .modules.yaml metadata"
    pkg_body = json.loads((fe / "package.json").read_text())
    direct = {**pkg_body.get("dependencies", {}), **pkg_body.get("devDependencies", {})}
    locked_versions = _locked_root_importer_versions((fe / "pnpm-lock.yaml").read_text())
    if locked_versions is None:
        return "unparseable pnpm-lock.yaml"
    for name, spec in direct.items():
        if isinstance(spec, str) and spec.startswith(("workspace:", "file:", "link:")):
            continue
        locked_version = locked_versions.get(name)
        if locked_version is None:
            return f"{name}: missing from pnpm-lock.yaml"
        if locked_version.startswith(("workspace:", "file:", "link:")):
            continue
        installed_pkg = node_modules / name / "package.json"
        if not installed_pkg.is_file():
            return f"{name}: not installed"
        installed_version = json.loads(installed_pkg.read_text()).get("version")
        if installed_version != locked_version:
            return f"{name}: installed {installed_version}, lockfile {locked_version}"
    return None


# meiva:note
# `install_argv` is the install command (`pnpm install --frozen-lockfile`) and `environ`
# the environment it inherits, with the Node search path injected; both come from the caller.
#
# Installs frontend deps when node_modules is missing OR when pnpm-lock.yaml changed since the
# last install — `pnpm run build` without the exact locked deps dies immediately and the session
# exits. `node_modules/.meiva-lock-hash` stamps the sha256 of the `pnpm-lock.yaml` last installed
# from (written after `pnpm install --frozen-lockfile`). When the stamp matches, `_node_modules_drift`
# verifies `.modules.yaml`, rejects foreign markers, and checks direct dependency versions against
# `pnpm-lock.yaml` (cf. docs/postmortems/0007).
def ensure_frontend_deps(
    repo: Path, *, environ: Mapping[str, str], install_argv: tuple[str, ...]
) -> None:
    fe = repo / "ui" / "web"
    node_modules = fe / "node_modules"
    stamp = node_modules / ".meiva-lock-hash"
    want = hashlib.sha256((fe / "pnpm-lock.yaml").read_bytes()).hexdigest()
    if node_modules.is_dir() and stamp.is_file() and stamp.read_text().strip() == want:
        drift = _node_modules_drift(fe, node_modules)
        if drift is None:
            return
        print(f"  · frontend deps drifted ({drift}), running pnpm install --frozen-lockfile")
    else:
        reason = "missing" if not node_modules.is_dir() else "pnpm-lock.yaml changed"
        print(f"  · frontend deps {reason}, running pnpm install --frozen-lockfile")
    subprocess.run(
        list(install_argv),
        cwd=str(fe),
        check=True,
        env=frontend_toolchain_env(environ),
    )
    stamp.write_text(want)


def assert_schema_current_or_die(db_url: str) -> int:
    """Verify the DB at `db_url` has applied exactly the code's required migration set. Targeted
    hints for the two failure shapes (DB behind code / code behind DB)."""
    from base.deploy.schema.migrations import (
        CodeBehindSchema,
        SchemaVersionMismatch,
        assert_schema_current,
    )

    try:
        assert_schema_current(db_url)
    except SchemaVersionMismatch as e:
        print(
            f"  ✗ {e}\n"
            f"    DB is behind the code in this checkout — the apply-migrations step of `meiva start` "
            f"should have caught up just before this check, so this likely means migrations/ in this "
            f"checkout was edited mid-flight.",
            file=sys.stderr,
        )
        return 1
    except CodeBehindSchema as e:
        print(
            f"  ✗ {e}\n"
            f"    The central DB is ahead of this code — typically the gateway moved to a newer "
            f"revision while this host stayed on an older one. Bring this host to the gateway's "
            f"revision (a source checkout: check out that commit and `uv sync`), then retry "
            f"`meiva start` with this unit's explicit service selection.",
            file=sys.stderr,
        )
        return 1
    print("  ✓ schema version matches code")
    return 0


# The one endpoint that answers "can a host use the gateway?". Authenticated, and
# deliberately **exempt from the paused-host 503 middleware**
# (`gateway/cluster/router.py`), so a 200 here means the gateway is *serving* — not
# that the cluster is unpaused. It is also served only after `gateway.app.main`'s
# `assert_schema_current` has passed, so serving implies migrated: the two are not
# separate instants a caller has to wait for in turn.
GATEWAY_PROBE_PATH = "/api/cluster/status"


class GatewayProbe(NamedTuple):
    """One dial of the gateway's probe endpoint, classified but not judged.

    `status` is the HTTP status, or None when the dial got no answer at all
    (connection refused / timeout — nothing is listening). `detail` is the exception
    text in that case, else the truncated response body.

    The agent-runner preflight (`probe_gateway_or_die`) uses this result to
    distinguish an unavailable gateway from a rejected request.
    """

    status: int | None
    detail: str


def probe_gateway_once(
    gateway_url: str, *, access: GatewayAccess, timeout_s: float = 10.0
) -> GatewayProbe:
    """Dial `GATEWAY_PROBE_PATH` on `gateway_url` once, with this cluster's auth.

    Classifies rather than decides: no printing, no retrying, no exit code. The
    callers differ entirely in what they do with a non-answer (the preflight gives up,
    the readiness gate keeps waiting), and only in that.
    """
    import httpx

    from base.cluster.machine import gateway_auth_headers
    from base.host.net.http_dial import get as dial_get

    try:
        resp = dial_get(
            f"{gateway_url.rstrip('/')}{GATEWAY_PROBE_PATH}",
            timeout=timeout_s,
            headers=gateway_auth_headers(access),
        )
    except httpx.HTTPError as e:
        return GatewayProbe(None, str(e))
    return GatewayProbe(resp.status_code, resp.text[:200])


# Gap between preflight dials. Small relative to the budget so a hole a few seconds
# wide is noticed a few seconds after it closes rather than at the next round number,
# and large enough that a genuinely dead gateway is reported in a handful of lines
# instead of a wall of them.
_PREFLIGHT_RETRY_INTERVAL_S = 5.0


def probe_gateway_or_die(
    gateway_url: str,
    *,
    access: GatewayAccess,
    timers: Timers,
    budget_s: float = GATEWAY_PREFLIGHT_BUDGET_S,
    retry_interval_s: float = _PREFLIGHT_RETRY_INTERVAL_S,
) -> int:
    """Probe the gateway from an agent-runner over the private network.

    The agent-runner's ops server is dialed *by* the gateway, but the host
    still reaches the gateway for watchdog self-heal updates and
    cluster status. Probe at `meiva start` time so a broken private-network path
    fails on stdout instead of only surfacing later during a self-heal.

    **Both transient shapes get the same bounded budget**: a 5xx (the gateway
    answered but is not ready) and no answer at all (nothing is listening on that
    address *yet*). A brief gateway restart can refuse a connection before
    serving returns; one refused packet is not evidence of a terminal failure.

    The budget buys nothing on the healthy path: a reachable gateway answers on the
    first dial and returns immediately. A gateway that is genuinely down still fails —
    `budget_s` is deliberately too short to outlast a real death, which needs the
    watchdog's own round to fix (`base.deploy.progress_timeout.GATEWAY_PREFLIGHT_BUDGET_S`).

    A non-200 the gateway *chose* to send (401/403/404) is terminal on the first dial
    as before: a credential or route mismatch is not a timing problem.
    """
    deadline = timers.monotonic() + budget_s
    attempt = 0
    while True:
        attempt += 1
        probe = probe_gateway_once(gateway_url, access=access)
        if probe.status == 200:
            print(f"  ✓ gateway reachable ({gateway_url})")
            return 0
        if probe.status is not None and probe.status < 500:
            print(
                f"  ✗ unexpected status {probe.status} from gateway: {probe.detail}.",
                file=sys.stderr,
            )
            return 1
        remaining = deadline - timers.monotonic()
        if remaining <= 0:
            if probe.status is None:
                print(
                    f"  ✗ gateway unreachable at {gateway_url} after {attempt} attempt(s) "
                    f"over {budget_s:.0f}s: {probe.detail}.\n"
                    f"    Check `MEIVA_GATEWAY_URL` in ~/.meiva/.env and that this host can "
                    f"reach the gateway over the cluster's private network.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  ✗ gateway returned {probe.status} at {GATEWAY_PROBE_PATH} after "
                    f"{attempt} attempt(s) over {budget_s:.0f}s: {probe.detail}.\n"
                    f"    Gateway may be mid-restart; retry later.",
                    file=sys.stderr,
                )
            return 1
        wait_s = min(retry_interval_s, remaining)
        shape = "unreachable" if probe.status is None else f"returned {probe.status}"
        print(
            f"  ⚠ gateway {shape} (attempt {attempt}, {remaining:.0f}s of budget left); "
            f"retrying in {wait_s:.0f}s…",
            file=sys.stderr,
        )
        timers.sleep(wait_s)


def register_machine_or_die(
    db: Database, resolved: SetupValues, roles: MachineRoles, *, command: CommandRuntime
) -> int:
    """UPSERT this host into the machines table with typed error handling.

    The dial URL comes from `base.cluster.machines.unit_dial_url(roles)` — the one
    definition, shared with the ops daemon's boot registration so the two writers
    of this row cannot advertise different addresses for the same unit.

    Failure modes get targeted hints + non-zero exit (a half-registered host
    silently breaks cross-machine orchestration; we'd rather fail loud on
    `meiva start`). The ops daemon's equivalent call is deliberately non-fatal
    instead — see `services/agent_runner/agent_ops/boot.py:register_boot`.
    """
    import psycopg

    from base.cluster.machines import LoopbackDialUrlRefused, register_self, unit_dial_url
    from base.daemon.endpoints import ServiceEndpoints
    from base.paths import run_dir

    endpoints = ServiceEndpoints.from_boot(command.config, runtime=run_dir(command.home))
    url = unit_dial_url(
        roles,
        host=command.machine.host(),
        ops_port=endpoints.of("ops").health_port,
        gateway_port=command.config.view.gateway.gateway_port,
        telemetry_otlp_port=command.config.view.observability.telemetry_otlp_port,
        gateway=command.config.view.gateway.gateway_url,
    )
    try:
        register_self(
            db,
            url=url,
            name=command.machine.name(),
            roles=roles,
            description=command.machine.description(),
            home=str(command.home),
            gateway=command.config.view.gateway.gateway_url,
        )
    except LoopbackDialUrlRefused as e:
        print(
            f"  ✗ {e}",
            file=sys.stderr,
        )
        return 1
    except psycopg.OperationalError as e:
        print(
            f"  ✗ register_self failed: cannot connect to central Postgres ({e}).\n"
            f"    Check `MEIVA_DB_URL` in ~/.meiva/.env points at the gateway's reachable Postgres "
            f"(public TLS endpoint or LAN). On agent-runner, also verify CF Tunnel / private "
            f"network connectivity to the gateway host.",
            file=sys.stderr,
        )
        return 1
    except psycopg.errors.UndefinedTable as e:
        print(
            f"  ✗ register_self failed: `machines` table does not exist ({e}).\n"
            f"    Gateway's DB schema is behind — run `meiva stop` then `meiva start` on the gateway, each with its explicit "
            f"choices (see their `--help`) (a source-run gateway applies pending migrations on a cold start), then retry "
            f"`meiva start` here (with this unit's explicit service selection).",
            file=sys.stderr,
        )
        return 1
    print(f"  ✓ {resolved['machine_name']} → {url or '(no dial url — station-only)'}")
    return 0


def preflight_probes(db: Database, *, command: CommandRuntime) -> int:
    """Run gateway and DB reachability checks BEFORE stopping services.

    Designed for `meiva restart` and the fleet update (self-update leg): validate
    that the host can still reach the gateway *before* killing its own
    services, so a transient gateway outage or network blip does not leave
    the host in a "services dead, can't start" state.

    Resolves the host's setup (gateway URL, machine name, roles) from the
    same persisted config that `_cmd_start_body` uses, then runs the same
    register + probe checks that `_cmd_start_body` runs *after* the stop.
    On failure the host keeps serving — the caller aborts without stopping.

    Returns 0 when both checks pass, non-zero otherwise.
    """

    from cli.commands._setup import collect_setup_values, missing_setup_message

    # Resolve setup from the home's persisted env/files (read-only, no writes).
    resolved, missing = collect_setup_values(config=command.config)
    if missing:
        print(missing_setup_message(missing), file=sys.stderr)
        return 1

    roles_raw = resolved.get("machine_role", "")
    roles: MachineRoles = frozenset(roles_raw.split(",")) if roles_raw else frozenset()

    print("\n→ preflight: register machine in central DB")
    rc = register_machine_or_die(db, resolved, roles, command=command)
    if rc != 0:
        print("  ✗ preflight failed: cannot register machine — host still serving", file=sys.stderr)
        return rc

    if "agent-runner" in roles and "gateway" not in roles:
        print("\n→ preflight: probe gateway")
        rc = probe_gateway_or_die(
            resolved["gateway_url"],
            access=gateway_access_from_boot(command.config),
            timers=command.timers,
        )
        if rc != 0:
            print("  ✗ preflight failed: gateway unreachable — host still serving", file=sys.stderr)
            return rc

    return 0
