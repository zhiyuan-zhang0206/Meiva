# meiva:note
# Machine API admission from the write generation.
#
# Machine callers present the generation's class token instead of the human
# cluster secret, so a machine holds no human bearer:
#
# - The generation's secret file carries one API token per class
#   (``GenerationSecret.api``). The gateway accepts the ACTIVE generation's
#   tokens (``acceptance``), never a pending one. A runner's ops server accepts
#   the gateway-class token only.
# - The root launcher delivers a service its class token in ``MEIVA_API_TOKEN``,
#   exactly like its database login, and only while the API is authenticated
#   (the gateway's human secret, which every gateway home holds; the unit
#   capability's API admission on a remote unit).
# - The OTLP relay ingress is telemetry, not a write path. Its bearer is a random
#   token minted once for the gateway home (``ensure_telemetry_token``) and kept
#   in a private file; remote units receive it in their capability. It is
#   independent of the human secret and of every write generation: neither
#   rotation changes it.
#
# Settings-free: the launcher, the boot pass and the gateway read the same
# private store. Comparisons are constant-time over SHA-256 digests, so the
# accepting side never needs a token in clear. A client reads its own token
# with `base.host.net.auth.delivered_token` (stdlib-only, safe during boot) and presents
# it through `base.cluster.machine.gateway_bearer`.

from __future__ import annotations

import hmac
from collections.abc import Mapping
from pathlib import Path

from base.host.db_authority.delivery import active_generation
from base.host.db_authority.digests import token_digest
from base.host.db_authority.ledger import authority_dir, ledger_path, load_ledger, read_secret
from base.host.db_authority.model import CLASSES, GenerationClass
from base.host.net.auth import API_TOKEN_ENV as API_TOKEN_ENV
from base.host.private_storage import write_private_bytes

_SCHEME = "Bearer "

# A caller-owned cache follows atomic ledger replacement on the next read.
type AcceptanceCache = dict[Path, tuple[tuple[int, int, int], dict[GenerationClass, str]]]

TELEMETRY_TOKEN_FILE = "telemetry-token"  # noqa: S105 — a file name, not a credential


def telemetry_token_path(home: Path) -> Path:
    """The private file the gateway home's root keeps the telemetry token in."""
    return authority_dir(home) / TELEMETRY_TOKEN_FILE


# meiva:note
# Keep the home's telemetry token: a published one stays, else `fresh` is published (0600).
#
# The token is the cluster's OTLP relay ingress bearer. Nothing derives it, so a rotated human
# secret or write generation never changes it and a remote unit's capability stays valid. The
# caller draws `fresh` (a start draws one every time and almost always discards it) so this
# function reads no random source and a test can place any value. The gateway home's converge
# step ensures it at every start; the gateway-side services that need the token (the heartbeat's
# observability-station probe) read the file with `read_telemetry_token`. A blank `fresh`
# raises before anything is read or written: a blank token would authenticate nobody and look
# published.
def ensure_telemetry_token(home: Path, fresh: str) -> None:
    if not fresh.strip():
        raise ValueError("a fresh telemetry token is non-blank")
    if read_telemetry_token(home) is None:
        write_private_bytes(telemetry_token_path(home), fresh.strip().encode())


def read_telemetry_token(home: Path) -> str | None:
    """The published telemetry token, or None when none was published (a start that has
    not run yet)."""
    try:
        body = telemetry_token_path(home).read_bytes()
    except FileNotFoundError:
        return None
    return body.decode().strip() or None


def bearer_class(
    authorization: str | None, accepted: Mapping[GenerationClass, str]
) -> GenerationClass | None:
    """The class whose token `authorization` presents as `Bearer <token>`, else None.

    Every accepted digest is compared in constant time, whatever matched.
    """
    if not accepted or not authorization or not authorization.startswith(_SCHEME):
        return None
    presented = token_digest(authorization[len(_SCHEME) :])
    matched: GenerationClass | None = None
    for cls, digest in accepted.items():
        if hmac.compare_digest(presented, digest):
            matched = cls
    return matched


def api_token(home: Path, cls: GenerationClass) -> str:
    """The active generation's `cls` API token (a pending one never)."""
    return read_secret(home, active_generation(home)).api.of(cls)


def acceptance(home: Path, *, cache: AcceptanceCache | None = None) -> dict[GenerationClass, str]:
    """Digests of the tokens the gateway at `home` accepts, by class.

    The ACTIVE generation's two tokens; none without a ledger (a remote-managed
    plane has no generations) or while no generation is active (a birth not yet
    admitted). A ledger or secret the store refuses raises. Gateway requests
    share their lifespan-owned cache; one-shot boot readers read without one.
    """
    path = ledger_path(home)
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    identity = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = None if cache is None else cache.get(home)
    if cached is not None and cached[0] == identity:
        return cached[1]
    ledger = load_ledger(home)
    accepted: dict[GenerationClass, str] = {}
    if ledger is not None and ledger.active is not None:
        tokens = read_secret(home, ledger.active).api
        accepted = {cls: token_digest(tokens.of(cls)) for cls in CLASSES}
    if cache is not None:
        cache[home] = (identity, accepted)
    return accepted
