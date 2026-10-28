# meiva:note
# Fetch cluster-common config from the gateway at process start.
#
# Every process on a configured pure agent-runner (serve_agent_runner flag on,
# enrolled with a gateway URL) fetches the cluster's config from the gateway at
# startup and injects it into os.environ before Settings is built — there is no
# materialized `.env` cache of cluster facts. The
# one cache that does exist is a transient 0600 config snapshot under
# `$MEIVA_HOME/run/`: a successful fetch writes it, and a later process skips the
# fetch while it is fresh, falling back to it (with a warning) when the gateway
# is unreachable — see the snapshot helpers below. A
# gateway-capable unit keeps the cluster's config in its own `$MEIVA_HOME/.env` and
# never fetches; which side a unit is on is derived from its serve flags (see
# `config_source_is_local` / `should_fetch_from_gateway`), not from an env var. A bare checkout with no role flags (CI, lint
# scripts) and a not-yet-enrolled runner resolve locally with no fetch — the preflight gate refuses an unenrolled
# `meiva start`, not the Settings import. The
# bytes travel the private network; an authenticated gateway requires a bearer,
# which this presents from the unit's machine API token (`MEIVA_API_TOKEN`, from
# its installed capability); a unit never holds the human cluster secret.
# Intentionally imports nothing from base.config (it runs DURING base.config
# import) — only stdlib and the settings-free EnvBootResult at import;
# the httpx / base.host.net.auth / base.host.net.http_dial
# pieces lazily at fetch time (all config-free, so they're safe this early in
# boot).
#
# Nothing here reads the process environment, the clock or the home on its own: the
# environment is the `env` mapping the caller passes (the config boot,
# `base.config._lite.ConfigBoot`, hands in `os.environ`), the home is a `Path`, the
# `.env` file is a mapping of its values (`base.host.env.dotenv_file.read_env_aliases`),
# and the time is a `now` float. A test passes dicts and a `tmp_path`.


from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

from base.host.atomic_io import write_bytes_atomic
from base.host.env.boot_policy import (
    BootstrapFetch,
    fetch_backoff,
    serve_flag,
    validated_bootstrap_payload,
)
from base.host.env.process_profile import (
    MEIVA_PROCESS_PROFILE_ENV,
    ProcessProfile,
    parse_process_profile,
)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


LAUNCHER_PROFILE_ENV_KEY = "MEIVA_LAUNCHER_PROFILE"


_GENERATION_ENV = "MEIVA_DB_GENERATION"
# meiva:note
# Environment key name for the unit API token, not a literal credential.
_API_TOKEN_ENV = "MEIVA_API_TOKEN"  # noqa: S105


# meiva:note
# Authority facts from one environment delivery, retained by its boot owner.
#
# A refusal is reported only when that owner's credential-free DB URL is
# dialed. Later environment deliveries cannot change this result.
@dataclass(frozen=True, slots=True)
class EnvBootResult:
    db_authority_refusal: str | None = None


# meiva:note
# The launcher's process profile for this process tree, live or recorded.
#
# A launcher-spawned daemon or agent carries the live `MEIVA_PROCESS_PROFILE`
# marker. A CLI entry point pops that marker before dispatch (the CLI is
# settings-full by design) after recording the value as
# `LAUNCHER_PROFILE_ENV_KEY` (cli/main.py `_normalize_process_profile`), so a
# boot pass reached through a CLI still knows the launcher context (#4334).
# None when the tree has no launcher context at all — a plain shell or test
# process — and the projection exemptions stay off. A value that is not a `ProcessProfile`
# raises (`parse_process_profile`): the marker is set by the launcher, and a misspelling must
# not pass for "some other profile" in the credential and projection branches that compare it.
def launcher_context(env: Mapping[str, str]) -> ProcessProfile | None:
    value = env.get(MEIVA_PROCESS_PROFILE_ENV) or env.get(LAUNCHER_PROFILE_ENV_KEY)
    return parse_process_profile(value) if value else None


# meiva:note
# What a process's launch environment says about the credential it presents to the gateway.
#
# Read once by the process's config boot (`ConfigBoot.launch_facts`) and handed to the
# consumers that decide on it (`GatewayAccess`): `machine_token` is the machine API token the
# launcher (or the boot pass, for an admitted operator) delivered; `launcher_profile` the
# launcher tree's profile, live or recorded; `gateway_home` whether this unit serves the
# gateway, so its config source is its own `.env`.
@dataclass(frozen=True)
class LaunchFacts:
    machine_token: str | None = field(repr=False)
    launcher_profile: ProcessProfile | None
    gateway_home: bool


# meiva:note
# The `LaunchFacts` of the environment `env` and the home's `.env` values `env_file`.
def launch_facts(env: Mapping[str, str], *, env_file: Mapping[str, str]) -> LaunchFacts:
    from base.host.net.auth import delivered_token

    return LaunchFacts(
        machine_token=delivered_token(env),
        launcher_profile=launcher_context(env),
        gateway_home=config_source_is_local(env, env_file=env_file),
    )


# meiva:note
# Whether `env` carries exactly this runner home's installed unit
# login (`base.host.db_authority.unit`) with its generation marker.
def is_delivered_unit_login(env: Mapping[str, str], *, home: Path) -> bool:
    generation = env.get(_GENERATION_ENV)
    home = home.expanduser().resolve()
    if not generation or not (home / "db-authority" / "unit.json").exists():
        return False
    from base.host.db_authority.unit import is_delivered_login

    return is_delivered_login(home, env.get("MEIVA_DB_URL"), generation)


# meiva:note
# Deliver the runner's login and API token before its bootstrap fetch.
#
# A launcher delivery of this home's installed capability is kept. An
# admitted operator consumes the installed capability. A refused process
# keeps the credential-free endpoint and returns its first-dial reason.
def deliver_unit_authority(env: MutableMapping[str, str], *, home: Path) -> EnvBootResult:
    if is_delivered_unit_login(env, home=home):
        return EnvBootResult()
    home = home.expanduser().resolve()
    context = launcher_context(env)
    if context is not None:
        # meiva:note
        # The refusal names the way out, because the marker is written by the platform's
        # session bootstrap and cannot be removed by the process that carries it (2026-10-28:
        # a seat respawner spent a night on this, then found unit_delivery/unit_api_delivery).
        return EnvBootResult(
            db_authority_refusal=(
                f"this {context}-profile agent-runner process was launched without its unit's "
                "database login; only the root launcher delivers it. Where this process is "
                f"launched, deliver MEIVA_DB_URL, {_GENERATION_ENV} and {_API_TOKEN_ENV} from "
                "this home's db-authority/unit.json "
                "(base.host.db_authority.unit.unit_delivery / unit_api_delivery); while a "
                "profile marker is set the boot path does not consume the installed login itself"
            )
        )
    from base.host.db_authority.model import AuthorityRefusedError
    from base.host.db_authority.unit import consume_unit

    try:
        capability = consume_unit(home)
    except (AuthorityRefusedError, ValueError, OSError) as exc:
        return EnvBootResult(
            db_authority_refusal=f"no database authority for this agent-runner process: {exc}"
        )
    env["MEIVA_DB_URL"] = capability.dsn
    env[_GENERATION_ENV] = str(capability.generation.number)
    env[_API_TOKEN_ENV] = capability.api.token
    return EnvBootResult()


def service_plugin_config_packet(env: Mapping[str, str]) -> str | None:
    """The launcher's fixed plugin config snapshot for this service generation.

    Separate from cluster bootstrap: a later bootstrap fetch must not replace
    the values captured by this unit's manifest and gate.
    """
    from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV

    return env.get(SERVICE_PLUGIN_CONFIG_ENV)


def cluster_plugin_config_values(env: Mapping[str, str], plugin: str) -> dict[str, object] | None:
    """One plugin's cluster projection from the existing bootstrap payload.

    This carrier never contains host fields or service birth snapshots. Schema
    admission belongs to the declared plugin class at its composition boundary.
    """
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    packet = env.get(PLUGIN_CLUSTER_CONFIG_ENV)
    if packet is None:
        return None
    raw: object = json.loads(packet)
    if not _is_object_dict(raw):
        raise TypeError("plugin cluster config must be a JSON object")
    if plugin not in raw:
        return None
    values = raw[plugin]
    if not _is_object_dict(values):
        raise TypeError(f"plugin cluster config {plugin!r} must be a JSON object")
    return values


# The parent config snapshot (P0 #2100): a successful fetch writes the payload
# to `$MEIVA_HOME/run/bootstrap-snapshot.json` (0600). A later process on the same
# unit — most importantly the exec child the agent process spawns per turn —
# SKIPS the fetch while the snapshot is fresh, so a gateway that is down or
# restarting is no longer a single point that silently kills every execute_code
# call. When the snapshot is stale the process fetches as before; if that fetch
# then fails on a transport error the process falls back to the snapshot anyway
# (last-known cluster config — the same values its parent already runs on),
# because during a gateway outage no cluster config edit can have happened
# since the snapshot was written.
_SNAPSHOT_NAME = "bootstrap-snapshot.json"
# Snapshots carry no database credential (bootstrap serves the credential-free
# endpoint); a snapshot of any other version is never reused.
SNAPSHOT_VERSION = 3


# Transport-level httpx failures mean "gateway unreachable / mid-restart" —
# the class that may fall back to a stale snapshot. Auth and status failures
# (401 wrong secret, 400, 5xx) still fail loud: a runner the gateway rejects
# must not keep running on old config.
def _transport_failures() -> tuple[type[BaseException], ...]:
    """Transport-level httpx failure classes (lazy import: httpx stays off the
    settings-import path until an actual fetch is attempted)."""
    import httpx

    return (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
    )


# The maintenance-verb opt-out (settings-lite). `meiva stop` / `meiva status` / the
# watchdog probe / the thin-client verbs must still construct Settings while the
# gateway is down, so `cli.main` sets MEIVA_CONFIG_FETCH=skip for them before any
# settings-loading import. Every other process — start, converge, update, the
# daemons, the agents — derives its fetch decision purely from the role flag and
# fetches when it is a pure agent-runner. base.sessions.env_forwarding deliberately does NOT
# forward this var to spawned processes (a daemon/agent must fetch per its own
# role, never inherit a CLI verb's opt-out).
CONFIG_FETCH_ENV = "MEIVA_CONFIG_FETCH"
CONFIG_FETCH_SKIP = "skip"


# meiva:note
# Whether this unit's config source is its own `$MEIVA_HOME/.env`.
#
# Role-derived (MEIVA_CONFIG_SOURCE deleted 2026-08-01): a unit that serves the
# gateway owns the cluster's config in its own `.env` and never fetches; a
# configured pure agent-runner holds only the bootstrap env (gateway URL +
# secret, from `meiva start`) and fetches the rest from the gateway at every
# process start (see `should_fetch_from_gateway`). A bare checkout with no
# role flags — CI, lint scripts, dev tools — is not a unit yet and resolves
# locally with no fetch.
#
# The serve-gateway flag is read settings-free (`env` > `env_file` > False)
# because this runs DURING the Settings import, before base.cluster.machine can
# resolve the role. `cli.main` opts the maintenance verbs out of the fetch with
# MEIVA_CONFIG_FETCH=skip (see CONFIG_FETCH_ENV); that is orthogonal to this
# derivation.
#
# `env` is the process environment (`os.environ` at a composition root) and
# `env_file` the home's `.env` values (`dotenv_file.read_env_aliases`).
def config_source_is_local(env: Mapping[str, str], *, env_file: Mapping[str, str]) -> bool:
    return serve_flag("MEIVA_MACHINE_SERVE_GATEWAY", env, env_file)


# meiva:note
# Whether this process's Settings build fetches cluster config from the gateway.
#
# True only for a CONFIGURED pure agent-runner: the serve_agent_runner flag is
# on (`env` `MEIVA_MACHINE_SERVE_AGENT_RUNNER` > `env_file` > False) AND a gateway
# URL is present (`meiva start` validates the remote projection before persisting
# runner identity). Everything else resolves locally:
#
# - a gateway-capable unit (config_source_is_local) never fetches;
# - a bare checkout with no role flags (CI, lint scripts, dev tools) and a
#   not-yet-enrolled runner (flag on, no URL) construct Settings from their
#   local env/.env with no fetch and no error — `meiva start`'s preflight gate
#   is what refuses an unenrolled runner, not the Settings import, so any
#   tool that imports base.config keeps working on any machine.
#
# The gateway URL is read from `env` only: by the time base.config calls this,
# load_meiva_env has loaded (and `_enforce_cluster_env_authority` forced) the
# unit's .env, so it is present in the environment when it exists on disk.
def should_fetch_from_gateway(env: Mapping[str, str], *, env_file: Mapping[str, str]) -> bool:
    return serve_flag("MEIVA_MACHINE_SERVE_AGENT_RUNNER", env, env_file) and bool(
        env.get("MEIVA_GATEWAY_URL")
    )


class BootstrapFetchError(RuntimeError):
    """A pure agent-runner could not fetch its cluster config from the gateway:
    no gateway URL to fetch from, or every fetch attempt failed (unreachable /
    401 / bad body).

    The process must not start with no config. `meiva start` exits 1 with this
    message (the boot policy retries it); a daemon dies at import and the OS
    watchdog probe revives it until the gateway is reachable again — the
    fetch's own retry plus that revive loop is what makes a gateway restart
    self-heal on a runner.
    """


# meiva:note
# GET {base_url}/api/bootstrap. Return {alias: value}.
#
# Retries with linear backoff (`fetch_backoff`) so a gateway that is briefly
# unreachable as the agent boots doesn't strand it.
#
# Bootstrap serves the credential-free database endpoint and no cluster
# secret. The runner's login comes only from its installed unit capability
# (`base.host.db_authority.unit`).
#
# Presents `Authorization: Bearer <bearer>` when `bearer` is given: a joining
# start holds its bundle's token, a booting process its delivered machine API
# token (`base.host.net.auth.delivered_token(env)`); without one the gateway
# answers 401. Never read from Settings — this runs during the Settings import.
#
# Raises:
#     httpx.HTTPError: every attempt failed (gateway unreachable / non-2xx, e.g.
#         401 when the machine token is missing, revoked or wrong).
#     TypeError: the response body is not a flat ``{str: str}`` map.
def fetch_bootstrap_config(
    base_url: str, *, bearer: str | None, tuning: BootstrapFetch
) -> dict[str, str]:
    import httpx

    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import get as dial_get
    from base.host.net.resilience import Policy, retry

    headers = bearer_header(bearer) if bearer else {}
    url = f"{base_url.rstrip('/')}/api/bootstrap"
    # Gateway briefly down: retry connect errors only; ReadTimeout propagates.
    # meiva:note
    # Linear backoff without Retry-After or jitter.
    policy = Policy(
        max_attempts=tuning.attempts,
        backoff=lambda attempt: fetch_backoff(attempt, tuning.backoff_step_s),
        jitter="none",
        jitter_span=1.0,
        classify=lambda exc: isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)),
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )

    def _fetch_once() -> dict[str, str]:
        resp = dial_get(url, timeout=tuning.timeout_s, headers=headers)
        resp.raise_for_status()
        return validated_bootstrap_payload(resp.json())

    return retry(policy)(_fetch_once)


# meiva:note
# The unit's config snapshot path, under `home`.
def snapshot_path(home: Path) -> Path:
    return home / "run" / _SNAPSHOT_NAME


# meiva:note
# The last-known cluster config and its age in seconds at `now`, or None.
#
# Guards: the snapshot must have been written for the SAME gateway URL (a
# re-enrolled runner must not reuse another gateway's values), carry the
# current version, and hold a flat ``{str: str}`` map. Absent / unreadable /
# malformed reads as None — the caller falls through to the fetch.
def read_config_snapshot(
    base_url: str, *, home: Path, now: float
) -> tuple[dict[str, str], float] | None:
    path = snapshot_path(home)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not _is_object_dict(raw):
        return None
    if raw.get("v") != SNAPSHOT_VERSION:
        return None
    if raw.get("base_url") != base_url:
        return None
    written_at = raw.get("written_at")
    if not isinstance(written_at, (int, float)):
        return None
    try:
        values = validated_bootstrap_payload(raw.get("values"))
    except TypeError:
        return None
    return values, max(0.0, now - float(written_at))


def _write_config_snapshot(
    base_url: str, values: dict[str, str], *, home: Path, now: float
) -> None:
    """Best-effort owner-only snapshot write (atomic replace).

    The snapshot carries cluster credentials, so it is 0600 like the rest of
    the unit's private storage. It never raises and never fails the boot: it
    is a cache, and the worst case of losing it is one extra fetch next time."""
    path = snapshot_path(home)
    payload = json.dumps(
        {
            "v": SNAPSHOT_VERSION,
            "base_url": base_url,
            "written_at": now,
            "values": values,
        },
        sort_keys=True,
    ).encode()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(path, payload, mode=0o600, suppress_cleanup_error=True)
    except OSError:
        return


# meiva:note
# Inject one bootstrap payload into `env` — the shared tail of the
# fetch path and the snapshot path.
def apply_bootstrap_values(
    env: MutableMapping[str, str], base_url: str, values: dict[str, str], *, home: Path
) -> None:
    # ``MEIVA_GATEWAY_HEALTH_URL`` is host-scoped, so the gateway correctly does
    # not serve it in the cluster bootstrap payload.  A pure runner that was
    # enrolled without an explicit override must still probe the gateway it
    # actually fetched from, not ServiceSettings' single-box localhost default.
    # Keep a deliberate host override; otherwise the enrollment base URL is the
    # one reachability fact this process already proved.
    if not env.get("MEIVA_GATEWAY_HEALTH_URL"):
        env["MEIVA_GATEWAY_HEALTH_URL"] = f"{base_url.rstrip('/')}/api/health"
    delivered = is_delivered_unit_login(env, home=home)
    for key, value in values.items():
        if key == "MEIVA_DB_URL" and delivered:
            # The launcher's (or this process's consumed) unit login stays; the
            # served value is only the credential-free endpoint.
            continue
        env[key] = value


def _gateway_base_url(env: Mapping[str, str]) -> str:
    """The enrolled gateway to dial, or an actionable failure.

    The gateway URL is MEIVA_GATEWAY_URL (`meiva start` wrote it to `.env`; this
    runs before Settings, so it is read from the environment directly).
    """
    base_url = env.get("MEIVA_GATEWAY_URL") or ""
    if not base_url:
        raise BootstrapFetchError(
            "this host is a pure agent-runner but has no MEIVA_GATEWAY_URL — its cluster "
            "config comes from the gateway at startup. Join it to its gateway first:\n"
            "    set MEIVA_DB_CAPABILITY_KEY from a non-echoing prompt, then run:\n"
            "    meiva init --serve-agent-runner --no-serve-gateway --gateway-url <url> --machine-name <name> --machine-host "
            "<this-host-addr> --db-capability <bundle from `meiva cluster db-authority issue-unit`>, then meiva start with this unit's explicit service selection"
        )
    return base_url


# meiva:note
# The cluster config values this process would run on — the boot resolution, read-only.
#
# The one resolution shared by `inject_config_from_gateway` (which applies
# the result to the environment) and callers that must ask "would this host boot
# against resolvable config right now?" without mutating their process — the
# hold watchdog's pre-attempt gate (task #4080). The contract is the boot
# contract, unchanged:
#
# - a fresh snapshot (`tuning.snapshot_fresh_s`) is authoritative and skips the
#   fetch entirely (P0 #2100);
# - otherwise a live fetch, refreshing the snapshot on success;
# - a transport failure falls back to the snapshot whatever its age — no
#   cluster edit can have landed while the gateway was unreachable — with a
#   logged warning; a non-transport failure (401/5xx) still fails loud.
#
# Raises:
#     BootstrapFetchError: no gateway URL, or every fetch attempt failed — the
#         process must not start with no config.
def resolve_bootstrap_values(
    env: Mapping[str, str], *, home: Path, now: float, tuning: BootstrapFetch
) -> dict[str, str]:
    from base.host.net.auth import delivered_token

    base_url = _gateway_base_url(env)
    snapshot = read_config_snapshot(base_url, home=home, now=now)
    if snapshot is not None and snapshot[1] <= tuning.snapshot_fresh_s:
        # Fresh parent snapshot: the authoritative values this unit fetched
        # moments ago. Skip the fetch — a gateway that is down or restarting
        # must not block this process (P0 #2100).
        return snapshot[0]
    try:
        # The gateway itself never fetches (config_source_is_local), so every
        # fetch this module makes is a runner fetch; it carries no database
        # login (the unit capability does).
        values = fetch_bootstrap_config(base_url, bearer=delivered_token(env), tuning=tuning)
    except _transport_failures() as exc:
        if snapshot is not None:
            # Gateway unreachable: continue on the last-known cluster config
            # (the same values the parent process already holds). A gateway
            # outage is exactly when exec children must keep running, and no
            # cluster edit can have landed since the gateway went down.
            from base.log import logger

            logger.warning(
                "[bootstrap] gateway unreachable ({exc}) — continuing on the "
                "last-known cluster config snapshot (age {age:.0f}s)",
                exc=type(exc).__name__,
                age=snapshot[1],
            )
            return snapshot[0]
        raise BootstrapFetchError(
            f"could not fetch cluster config from the gateway at {base_url} ({exc}).\n"
            "    A pure agent-runner fetches GET /api/bootstrap at every process start "
            "(a fresh parent snapshot skips the fetch); this host has no snapshot to "
            "fall back on. Bring the gateway up (or check MEIVA_GATEWAY_URL / the "
            "unit capability's API token / private-network reachability), then retry."
        ) from exc
    except Exception as exc:
        raise BootstrapFetchError(
            f"could not fetch cluster config from the gateway at {base_url} ({exc}).\n"
            "    A pure agent-runner fetches GET /api/bootstrap at every process start "
            "(a fresh parent snapshot skips the fetch). Bring the gateway up (or check "
            "MEIVA_GATEWAY_URL / the unit capability's API token / private-network "
            "reachability), then retry."
        ) from exc
    _write_config_snapshot(base_url, values, home=home, now=now)
    return values


# meiva:note
# Fetch the cluster's config from the gateway and inject it into `env`.
#
# Runs at Settings construction on a pure agent-runner (see
# `config_source_is_local`); the config boot passes `os.environ`. The fetched
# values are AUTHORITATIVE: every key the gateway serves overwrites `env`,
# including a stale value a `.env` still carries (`_enforce_cluster_env_authority`
# forces file values in at `load_meiva_env`, and this runs after it) and a
# forwarded copy from a spawning process. The gateway's `.env` is the single
# cluster-wide copy.
#
# The values come from `resolve_bootstrap_values` (fresh snapshot / fetch /
# last-known-snapshot fallback); this wrapper applies them, including the
# derived `MEIVA_GATEWAY_HEALTH_URL` for a runner enrolled without an explicit
# override. The database login is the one exception to "fetched values are
# authoritative": a delivered unit login is kept, and a process without one
# receives the installed capability only when it runs the admitted runtime
# (`deliver_unit_authority`). That delivery runs first: it also supplies the
# machine API token the fetch authenticates with.
def inject_config_from_gateway(
    env: MutableMapping[str, str], *, home: Path, now: float, tuning: BootstrapFetch
) -> EnvBootResult:
    base_url = _gateway_base_url(env)
    result = deliver_unit_authority(env, home=home)
    values = resolve_bootstrap_values(env, home=home, now=now, tuning=tuning)
    apply_bootstrap_values(env, base_url, values, home=home)
    return result
