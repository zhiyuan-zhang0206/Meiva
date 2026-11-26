"""Resolve cluster config before Settings exists.

Gateway-capable units own their configuration in their local dotenv. Enrolled
pure runners receive configuration from the authenticated bootstrap endpoint
or the signed, host-bound local mirror. The environment, home and boot's
wall-clock sample are supplied by the caller.
"""

from __future__ import annotations

import json
import stat
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, TypeGuard

from base.host.atomic_io import SYSTEM_FS, AtomicWriter, write_bytes_atomic
from base.host.boundary.untyped import object_of
from base.host.env.boot_policy import (
    MIRROR_HOST_PARAM,
    BootstrapEnvelope,
    BootstrapFetch,
    MirrorRejectedError,
    fetch_backoff,
    serve_flag,
    validate_config_mirror,
)
from base.host.env.boot_policy import (
    validated_bootstrap_config as validated_bootstrap_config,
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
    bootstrap_source: Literal["live_bootstrap", "authorized_mirror", "legacy_fallback"] | None = (
        None
    )
    bootstrap_generation: str | None = None


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
#
# `home` is the resolved home the boot entry read (base/config/_lite.py); this
# canonicalizes it, never expands a tilde against the process's user database.
def is_delivered_unit_login(env: Mapping[str, str], *, home: Path) -> bool:
    generation = env.get(_GENERATION_ENV)
    home = home.resolve()
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
    home = home.resolve()
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
    raw = object_of(json.loads, packet)
    if not _is_object_dict(raw):
        raise TypeError("plugin cluster config must be a JSON object")
    if plugin not in raw:
        return None
    values = raw[plugin]
    if not _is_object_dict(values):
        raise TypeError(f"plugin cluster config {plugin!r} must be a JSON object")
    return values


# meiva:link base/host/env/docs/bootstrap-mirror.md
_SNAPSHOT_NAME = "bootstrap-snapshot.json"
# Version 4 invalidates caches that contain retired Fleet task fields.
SNAPSHOT_VERSION = 4


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
    """The runner cannot resolve cluster config for this boot invocation."""

    def __init__(
        self, reason: str, *, mirror_state: Literal["invalid", "expired"] | None = None
    ) -> None:
        self.mirror_state = mirror_state
        super().__init__(reason)


def fetch_bootstrap_config(
    base_url: str, *, bearer: str | None, tuning: BootstrapFetch
) -> dict[str, str]:
    """Fetch the flat projection used by enrollment and older clients."""
    return validated_bootstrap_config(
        _fetch_bootstrap_payload(base_url, bearer=bearer, machine_host=None, tuning=tuning)
    )


def _fetch_bootstrap_payload(
    base_url: str, *, bearer: str | None, machine_host: str | None, tuning: BootstrapFetch
) -> object:
    import httpx

    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import get as dial_get
    from base.host.net.resilience import Policy, retry
    from base.log import guard_default_logging

    guard_default_logging()
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

    params = {MIRROR_HOST_PARAM: machine_host} if machine_host else None

    def _fetch_once() -> object:
        resp = dial_get(url, timeout=tuning.timeout_s, headers=headers, params=params)
        _ = resp.raise_for_status()
        return object_of(resp.json)

    return retry(policy)(_fetch_once)


# meiva:link base/host/env/docs/bootstrap-mirror.md
def _mirror_path(home: Path) -> Path:
    return home / "run" / "bootstrap-mirror.json"


def read_config_mirror(
    *, home: Path, bearer: str, machine_host: str, now: float
) -> BootstrapEnvelope | None:
    """Read and authenticate the installed mirror, or return None when absent."""
    path = _mirror_path(home)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError:
        raise MirrorRejectedError("could not inspect the installed mirror") from None
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise MirrorRejectedError("installed mirror must be a regular 0600 file")
    try:
        payload = object_of(json.loads, path.read_bytes())
    except (OSError, ValueError):
        raise MirrorRejectedError("could not read the installed mirror JSON") from None
    return validate_config_mirror(payload, bearer=bearer, machine_host=machine_host, now=now)


def stage_config_mirror(
    payload: object,
    *,
    home: Path,
    bearer: str,
    machine_host: str,
    now: float,
    writer: AtomicWriter | None = None,
) -> BootstrapEnvelope:
    """Verify a gateway response and publish it atomically, including during pre-stage."""
    envelope = validate_config_mirror(payload, bearer=bearer, machine_host=machine_host, now=now)
    path = _mirror_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = AtomicWriter(SYSTEM_FS) if writer is None else writer
    writer.write_bytes(path, json.dumps(envelope, sort_keys=True).encode(), mode=0o600)
    return envelope


def snapshot_path(home: Path) -> Path:
    return home / "run" / _SNAPSHOT_NAME


# meiva:note
# The last-known cluster config and its age in seconds at `now`, or None.
#
# Guards: the snapshot must have been written for the SAME gateway URL (a
# re-enrolled runner must not reuse another gateway's values), carry the
# current version, and pass the same payload validation as a live response.
# An unusable snapshot falls through to the fetch.
def read_config_snapshot(
    base_url: str, *, home: Path, now: float
) -> tuple[dict[str, str], float] | None:
    path = snapshot_path(home)
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            return None
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _is_object_dict(raw):
        return None
    if raw.get("v") != SNAPSHOT_VERSION:
        return None
    if raw.get("base_url") != base_url:
        return None
    written_at = raw.get("written_at")
    if (
        isinstance(written_at, bool)
        or not isinstance(written_at, (int, float))
        or not 0 <= written_at <= now
    ):
        return None
    try:
        values = validated_bootstrap_config(raw.get("values"))
    except (TypeError, ValueError):
        return None
    return values, now - written_at


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


@dataclass(frozen=True, slots=True)
class BootstrapResolution:
    values: dict[str, str] = field(repr=False)
    source: Literal["live_bootstrap", "authorized_mirror", "legacy_fallback"]
    generation: str | None = None


def _mirror_resolution(envelope: BootstrapEnvelope) -> BootstrapResolution:
    return BootstrapResolution(envelope["values"], "authorized_mirror", envelope["generation"])


def _read_mirror(
    *, home: Path, bearer: str, machine_host: str, now: float
) -> BootstrapEnvelope | MirrorRejectedError | None:
    try:
        return read_config_mirror(home=home, bearer=bearer, machine_host=machine_host, now=now)
    except MirrorRejectedError as exc:
        return exc


def _offline_resolution(
    mirror: BootstrapEnvelope | MirrorRejectedError | None,
    legacy: tuple[dict[str, str], float] | None,
    *,
    base_url: str,
    now: float,
) -> BootstrapResolution:
    from base.log import logger

    if isinstance(mirror, MirrorRejectedError):
        raise BootstrapFetchError(
            f"cannot boot offline: {mirror.state} configuration mirror ({mirror})",
            mirror_state=mirror.state,
        )
    if mirror is not None:
        if now >= mirror["expires_at"]:
            raise BootstrapFetchError(
                "cannot boot offline: configuration mirror expired during refresh",
                mirror_state="expired",
            )
        return _mirror_resolution(mirror)
    if legacy is not None:
        logger.warning("[bootstrap] gateway unreachable; using the legacy bootstrap snapshot")
        return BootstrapResolution(legacy[0], "legacy_fallback")
    raise BootstrapFetchError(
        f"could not fetch cluster config from {base_url}; this runner has no snapshot "
        "or authorized mirror. Check gateway reachability and retry."
    )


def _accept_live(
    payload: object,
    *,
    home: Path,
    env: Mapping[str, str],
    now: float,
    started: float,
    allow_legacy: bool,
) -> BootstrapResolution:
    from base.host.net.auth import delivered_token
    from base.log import logger

    # Legacy responses contain only environment aliases. Any envelope-shaped
    # response must satisfy the signed contract before its values can be used.
    if _is_object_dict(payload) and not {"schema_version", "values", "integrity"} & payload.keys():
        if not allow_legacy:
            raise MirrorRejectedError(
                "gateway returned an unsigned response after mirror enrollment"
            )
        values = validated_bootstrap_config(payload)
        _write_config_snapshot(_gateway_base_url(env), values, home=home, now=now)
        return BootstrapResolution(values, "live_bootstrap")

    bearer = delivered_token(env) or ""
    machine_host = env.get("MEIVA_MACHINE_HOST", "")
    now += time.monotonic() - started
    try:
        envelope = stage_config_mirror(
            payload, home=home, bearer=bearer, machine_host=machine_host, now=now
        )
    except OSError as exc:
        # The authenticated live values can still boot this process. Publication
        # failed before rename, so the previously installed mirror stays in place.
        logger.warning(
            "[bootstrap] could not persist configuration mirror ({kind})", kind=type(exc).__name__
        )
        envelope = validate_config_mirror(
            payload, bearer=bearer, machine_host=machine_host, now=now
        )
    return BootstrapResolution(envelope["values"], "live_bootstrap", envelope["generation"])


def resolve_bootstrap_config(
    env: Mapping[str, str], *, home: Path, now: float, tuning: BootstrapFetch
) -> BootstrapResolution:
    """Resolve the boot projection and expose which source authorized it."""
    import httpx

    from base.host.net.auth import delivered_token
    from base.log import guard_default_logging

    guard_default_logging()
    base_url = _gateway_base_url(env)
    bearer = delivered_token(env) or ""
    machine_host = env.get("MEIVA_MACHINE_HOST", "")
    mirror = _read_mirror(home=home, bearer=bearer, machine_host=machine_host, now=now)
    if (
        mirror is not None
        and not isinstance(mirror, MirrorRejectedError)
        and now - mirror["issued_at"] <= tuning.snapshot_fresh_s
    ):
        return _mirror_resolution(mirror)
    legacy = read_config_snapshot(base_url, home=home, now=now) if mirror is None else None
    if legacy is not None and legacy[1] <= tuning.snapshot_fresh_s:
        return BootstrapResolution(legacy[0], "legacy_fallback")

    started = time.monotonic()
    try:
        payload = _fetch_bootstrap_payload(
            base_url, bearer=bearer, machine_host=machine_host if bearer else None, tuning=tuning
        )
        return _accept_live(
            payload,
            home=home,
            env=env,
            now=now,
            started=started,
            allow_legacy=mirror is None,
        )
    except _transport_failures():
        return _offline_resolution(
            mirror, legacy, base_url=base_url, now=now + (time.monotonic() - started)
        )
    except MirrorRejectedError as exc:
        raise BootstrapFetchError(
            f"gateway bootstrap rejected: {exc}", mirror_state=exc.state
        ) from None
    except httpx.HTTPStatusError as exc:
        raise BootstrapFetchError(
            f"gateway bootstrap failed with HTTP {exc.response.status_code}"
        ) from None
    except Exception as exc:
        raise BootstrapFetchError(f"could not use gateway bootstrap: {exc}") from None


def resolve_bootstrap_values(
    env: Mapping[str, str], *, home: Path, now: float, tuning: BootstrapFetch
) -> dict[str, str]:
    """Resolve the same projection as runner boot without mutating the environment."""
    return resolve_bootstrap_config(env, home=home, now=now, tuning=tuning).values


def inject_config_from_gateway(
    env: MutableMapping[str, str], *, home: Path, now: float, tuning: BootstrapFetch
) -> EnvBootResult:
    """Deliver the unit login and apply the resolved cluster projection to a runner."""
    base_url = _gateway_base_url(env)
    result = deliver_unit_authority(env, home=home)
    resolved = resolve_bootstrap_config(env, home=home, now=now, tuning=tuning)
    apply_bootstrap_values(env, base_url, resolved.values, home=home)
    return replace(
        result, bootstrap_source=resolved.source, bootstrap_generation=resolved.generation
    )
