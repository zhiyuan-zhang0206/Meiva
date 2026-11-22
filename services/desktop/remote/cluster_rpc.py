from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from pathlib import Path

import httpx

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.permissions_helper.client import PermissionsHelperError
from base.paths import logs_dir

from .errors import CornerFailsafeError
from .plan import CORNER_FAILSAFE_CODE

_DEFAULT_RPC_TIMEOUT_S = 60.0
_DEFAULT_PROBE_TIMEOUT_S = 2.5
_MAX_IMAGE_BYTES = 24 * 1024 * 1024
_MAX_IMAGE_B64_CHARS = ((_MAX_IMAGE_BYTES + 2) // 3) * 4 + 4
_ALLOWED_IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})


async def probe_cluster_computer_status(
    ops_url: str,
    *,
    headers: Mapping[str, str],
    timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    url = f"{ops_url.rstrip('/')}/computer/rpc"
    own = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=timeout_s, trust_env=False)
    try:
        resp = await http.post(url, json={"action": "status"}, headers=dict(headers))
        if resp.status_code != 200:
            return {"ok": False, "available": False, "error": f"HTTP {resp.status_code}"}
        parsed = object_of(resp.json)
        if not is_object_dict(parsed):
            return {"ok": False, "available": False, "error": "invalid status payload"}
        return parsed
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "available": False, "error": str(exc)}
    finally:
        if own:
            await http.aclose()


def _post_cluster_rpc(
    ops_url: str,
    body: dict[str, object],
    *,
    headers: Mapping[str, str],
    timeout_s: float,
    client: httpx.Client | None,
) -> dict[str, object]:
    url = f"{ops_url.rstrip('/')}/computer/rpc"
    own = client is None
    http = client if client is not None else httpx.Client(timeout=timeout_s, trust_env=False)
    try:
        resp = http.post(url, json=body, headers=dict(headers))
    except httpx.HTTPError as exc:
        raise PermissionsHelperError(
            f"cluster desktop peer unreachable at {ops_url}: {exc}"
        ) from exc
    finally:
        if own:
            http.close()
    if resp.status_code == 401:
        raise PermissionsHelperError("cluster desktop peer rejected authentication (HTTP 401)")
    if resp.status_code >= 400:
        raise PermissionsHelperError(
            f"cluster desktop peer returned HTTP {resp.status_code}: {resp.text.strip()[:300]}"
        )
    try:
        parsed = object_of(resp.json)
    except ValueError as exc:
        raise PermissionsHelperError(
            f"cluster desktop peer returned malformed JSON: {exc}"
        ) from exc
    if not is_object_dict(parsed):
        raise PermissionsHelperError("cluster desktop peer response is not a JSON object")
    return parsed


def _validated_image_ext(raw_ext: object) -> str:
    if raw_ext is None:
        return ".png"
    if isinstance(raw_ext, str) and raw_ext.lower() in _ALLOWED_IMAGE_EXTS:
        return raw_ext.lower()
    raise PermissionsHelperError(
        f"cluster desktop peer returned disallowed screenshot extension: {raw_ext!r}"
    )


def _save_remote_snapshot(
    out: dict[str, object], *, agent_id: int | None, stamp: str, home: Path
) -> None:
    raw_b64 = out.pop("_remote_image_b64", None)
    raw_ext = out.pop("_remote_image_ext", None)
    if not isinstance(raw_b64, str) or not raw_b64:
        return
    if len(raw_b64) > _MAX_IMAGE_B64_CHARS:
        raise PermissionsHelperError(
            f"cluster desktop peer screenshot payload exceeds {_MAX_IMAGE_BYTES} byte limit"
        )
    ext = _validated_image_ext(raw_ext)
    try:
        img_bytes = base64.b64decode(raw_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PermissionsHelperError(
            f"cluster desktop peer returned invalid screenshot bytes: {exc}"
        ) from exc
    if len(img_bytes) > _MAX_IMAGE_BYTES:
        raise PermissionsHelperError(
            f"cluster desktop peer screenshot exceeds {_MAX_IMAGE_BYTES} byte limit"
        )
    directory = (logs_dir(home=home) / "computer" / "snapshots").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    local_path = (directory / f"agent-{agent_id or 0}-{stamp}{ext}").resolve()
    if not local_path.is_relative_to(directory):
        raise PermissionsHelperError(
            "cluster desktop peer screenshot path escapes snapshot directory"
        )
    _ = local_path.write_bytes(img_bytes)
    out["path"] = str(local_path)


def call_cluster_computer_tool(
    ops_url: str,
    *,
    headers: Mapping[str, str],
    tool: str,
    args: Mapping[str, object],
    agent_id: int | None,
    stamp: str,
    home: Path,
    timeout_s: float = _DEFAULT_RPC_TIMEOUT_S,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "action": "call_tool",
        "tool": tool,
        "args": {k: v for k, v in args.items() if k != "host"},
        "agent_id": agent_id,
    }
    parsed = _post_cluster_rpc(ops_url, body, headers=headers, timeout_s=timeout_s, client=client)
    if parsed.get("ok") is not True:
        err = str(parsed.get("error") or "cluster desktop tool failed")
        if CORNER_FAILSAFE_CODE in err or "Stopped by the user" in err:
            raise CornerFailsafeError(err)
        raise PermissionsHelperError(err)
    result = parsed.get("result")
    if not is_object_dict(result):
        raise PermissionsHelperError("cluster desktop peer reply missing result object")
    out = dict(result)
    _save_remote_snapshot(out, agent_id=agent_id, stamp=stamp, home=home)
    return out
