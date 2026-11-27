# meiva:note
# The pure decisions of the config boot: which flag spelling is on, how long a connect retry
# waits, what a bootstrap payload must look like, how a fetch is tuned.
#
# Values in, values out: nothing here reads the environment, a file, the clock or the network.
# `base.host.env.bootstrap` is the shell that reads them and executes the plan.


from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict, TypeGuard

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

__all__ = [
    "MIRROR_HOST_PARAM",
    "PLACEHOLDER_DB_URL",
    "PLUGIN_CLUSTER_CONFIG_ENV",
    "BootstrapEnvelope",
    "BootstrapFetch",
    "MirrorRejectedError",
    "fetch_backoff",
    "issue_config_mirror",
    "serve_flag",
    "validate_config_mirror",
    "validated_bootstrap_config",
    "validated_bootstrap_payload",
]

# meiva:note
# Planted as MEIVA_DB_URL when a boot has no cluster connection facts (the lite
# boot's placeholders, `base/config/_lite.py`). A syntactically valid URL that
# can never reach a real database (port 1 on loopback), so a stray connection
# fails loudly instead of silently hitting a database a host `.env` points at.
# base/db.connect() detects it and raises an actionable error.
PLACEHOLDER_DB_URL = "postgresql://no-cluster@127.0.0.1:1/run-meiva-start-first"

PLUGIN_CLUSTER_CONFIG_ENV = "MEIVA_PLUGIN_CLUSTER_CONFIG"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


# meiva:note
# An agent boots by fetching this. The timeout must cover a slow-but-healthy
# fetch under load (the whole boot -- fetch + import + claim -- has to finish
# before the parent's launch-confirm poll gives up; too small a cap fails a
# fetch that would have succeeded -- a 3s cap regressed CI). The retry covers a
# gateway briefly unreachable mid-restart (connection refused), which fails fast.
# A ReadTimeout (slow response) is deliberately NOT retried: stacking another
# full timeout would blow past the launch-confirm window and fail a spawn whose
# first fetch would have eventually returned.
# Tuning of one bootstrap fetch; every default is the production value.
#
# The config boot passes `BootstrapFetch()`; a test passes small values.
# `backoff_step_s` is the linear connect-retry step (`fetch_backoff`), and
# `snapshot_fresh_s` how long a parent snapshot stands in for a live fetch: it
# bounds cluster-edit propagation to a few minutes in steady state (the first
# child past the window re-fetches and refreshes the snapshot) while keeping the
# common per-turn child boot fetch-free.
@dataclass(frozen=True)
class BootstrapFetch:
    timeout_s: float = 10.0
    attempts: int = 2
    backoff_step_s: float = 0.5
    snapshot_fresh_s: float = 300.0


# meiva:note
# Seconds to wait after failed attempt number `attempt` (0-based): linear in the attempt.
def fetch_backoff(attempt: int, step_s: float) -> float:
    return step_s * (attempt + 1)


# meiva:note
# Settings-free mirror of one capability flag's resolution: `env`
# `MEIVA_MACHINE_SERVE_*` > the home's `.env` values (`env_file`) > False.
#
# Normal config boot has already loaded the `.env` into the environment before
# this runs, so its value is in `env`. Settings-lite maintenance commands do
# not load it: they pass the file's values too, so `meiva config --local`
# correctly sees a gateway unit without constructing Settings.
# `base.cluster.machine` reads the flag through Settings, which does not exist
# yet while this module runs. A malformed value resolves False here; the real
# resolver raises on it loudly later, so the config-source decision can never
# silently disagree with the machine role.
def serve_flag(env_key: str, env: Mapping[str, str], env_file: Mapping[str, str]) -> bool:
    raw = env.get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    raw = env_file.get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    return False


# meiva:note
# Assert the bootstrap body is a flat ``{str: str}`` map before the caller
# writes it into the environment.
#
# A gateway version skew, a wrong endpoint, or a proxy substituting the body
# could feed a non-str value that either raises an opaque ``TypeError`` deep in
# ``os.environ.__setitem__`` or (for a str-able scalar) writes a value the
# recipient never round-trips. Fail loud at the trust boundary instead.
def validated_bootstrap_payload(payload: object) -> dict[str, str]:
    if not _is_object_dict(payload):
        raise TypeError(f"/api/bootstrap returned {type(payload).__name__}, expected a JSON object")
    out: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                "/api/bootstrap must be a flat {str: str} map; got "
                f"{type(key).__name__} key -> {type(value).__name__} value for {key!r}"
            )
        out[key] = value
    return out


def validated_bootstrap_config(
    payload: object, *, require_plugin_config: bool = False
) -> dict[str, str]:
    from base.host.env.config_lite_table import BUILTIN_CLUSTER_CONFIG_FIELDS

    values = validated_bootstrap_payload(payload)
    packet = values.get(PLUGIN_CLUSTER_CONFIG_ENV)
    if packet is None and not require_plugin_config:
        return values
    raw = None if packet is None else object_of(json.loads, packet)
    if not is_object_dict(raw):
        raise TypeError("bootstrap plugin cluster config must be a JSON object")
    for plugin, fields in BUILTIN_CLUSTER_CONFIG_FIELDS.items():
        projection = raw.get(plugin)
        if not is_object_dict(projection) or projection.keys() != fields:
            raise ValueError(
                f"bootstrap cluster fields for plugin {plugin!r} differ from declaration"
            )
    return values


# meiva:link base/host/env/docs/bootstrap-mirror.md
MIRROR_HOST_PARAM = "mirror_machine_host"
_SCHEMA_VERSION = 1
_ROLE = "agent-runner"
_LIFETIME_S = 24 * 60 * 60
_CLOCK_SKEW_S = 30
_MAC_DOMAIN = b"meiva/bootstrap-mirror/v1\0"


class _Claims(TypedDict):
    schema_version: int
    generation: str
    issuer: str
    machine_host: str
    role: str
    issued_at: int
    expires_at: int
    values: dict[str, str]


class BootstrapEnvelope(_Claims):
    integrity: str


class MirrorRejectedError(ValueError):
    def __init__(self, reason: str, *, state: Literal["invalid", "expired"] = "invalid") -> None:
        self.state: Literal["invalid", "expired"] = state
        super().__init__(reason)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _mac(bearer: str, purpose: bytes, body: bytes) -> str:
    return hmac.digest(bearer.encode(), _MAC_DOMAIN + purpose + body, "sha256").hex()


def _claims(values: dict[str, str], *, bearer: str, machine_host: str, issued_at: int) -> _Claims:
    return _Claims(
        schema_version=_SCHEMA_VERSION,
        generation=hashlib.sha256(_json(values)).hexdigest(),
        issuer=_mac(bearer, b"issuer", b""),
        machine_host=machine_host,
        role=_ROLE,
        issued_at=issued_at,
        expires_at=issued_at + _LIFETIME_S,
        values=values,
    )


def issue_config_mirror(
    values: dict[str, str], *, bearer: str, machine_host: str, now: float
) -> BootstrapEnvelope:
    if not bearer or not machine_host.strip():
        raise MirrorRejectedError(
            "mirror signing requires an enrolled runner token and machine host"
        )
    claims = _claims(values, bearer=bearer, machine_host=machine_host, issued_at=int(now))
    return BootstrapEnvelope(**claims, integrity=_mac(bearer, b"claims", _json(claims)))


def validate_config_mirror(
    payload: object, *, bearer: str, machine_host: str, now: float
) -> BootstrapEnvelope:
    if not is_object_dict(payload) or type(payload.get("schema_version")) is not int:
        raise MirrorRejectedError("missing or malformed mirror schema")
    expected = _validate_mirror_claims(payload, bearer=bearer, machine_host=machine_host, now=now)
    integrity = payload.get("integrity")
    if not bearer or not isinstance(integrity, str) or not integrity.isascii():
        raise MirrorRejectedError("missing or malformed mirror integrity proof")
    if not hmac.compare_digest(integrity, _mac(bearer, b"claims", _json(expected))):
        raise MirrorRejectedError("mirror integrity check failed")
    envelope = BootstrapEnvelope(**expected, integrity=integrity)
    if payload != envelope or type(payload.get("expires_at")) is not int:
        raise MirrorRejectedError("mirror claims do not match the authenticated projection")
    if now >= envelope["expires_at"]:
        raise MirrorRejectedError("mirror authorization has expired", state="expired")
    return envelope


def _validate_mirror_claims(
    payload: dict[str, object], *, bearer: str, machine_host: str, now: float
) -> _Claims:
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise MirrorRejectedError("unsupported mirror schema")
    if (
        not machine_host
        or payload.get("machine_host") != machine_host
        or payload.get("role") != _ROLE
    ):
        raise MirrorRejectedError("mirror host or role does not match this runner")
    issued_at = payload.get("issued_at")
    if (
        type(issued_at) is not int
        or not math.isfinite(now)
        or not 0 <= issued_at <= now + _CLOCK_SKEW_S
    ):
        raise MirrorRejectedError("invalid mirror issue time")
    try:
        values = validated_bootstrap_config(payload.get("values"), require_plugin_config=True)
    except (TypeError, ValueError, RecursionError):
        raise MirrorRejectedError("invalid mirror configuration projection") from None
    return _claims(values, bearer=bearer, machine_host=machine_host, issued_at=issued_at)
