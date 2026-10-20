# meiva:note
# The CLI's door to the cluster extension registry is the gateway's HTTP API; the CLI holds no
# database handle for it. See gateway/extensions/docs/registry.meiva.okf.md.
from __future__ import annotations

import base64
from pathlib import Path
from typing import cast

import httpx

from base.cluster.machine import (
    GatewayAccess,
    GatewayApiBaseMissing,
    GatewayApiTokenMissing,
    gateway_api_base,
    gateway_auth_headers,
)
from base.host.net import http_dial
from base.packages.extensions.registry import ExtensionKind, pack_tree
from cli.commands.extensions.skill_package import ExtensionRegistryError

_TIMEOUT_S = 60.0


class HttpExtensionGateway:
    def __init__(self, access: GatewayAccess) -> None:
        self._access = access

    def put_extension(
        self, *, kind: ExtensionKind, name: str, root: Path, source: str, source_ref: str | None
    ) -> None:
        body = {
            "source": source,
            "source_ref": source_ref,
            "archive_b64": base64.b64encode(pack_tree(root)).decode("ascii"),
        }
        try:
            url = f"{gateway_api_base(self._access.gateway_url)}/api/extensions/{kind}/{name}"
            response = http_dial.put(
                url,
                json=body,
                timeout=_TIMEOUT_S,
                headers=gateway_auth_headers(self._access),
            )
        except (httpx.HTTPError, GatewayApiBaseMissing, GatewayApiTokenMissing) as exc:
            raise ExtensionRegistryError(
                f"cannot reach the cluster gateway to register {name!r}: {exc}. "
                "Bring the cluster up and re-run."
            ) from exc
        if response.is_success:
            return
        raise ExtensionRegistryError(
            f"the cluster gateway refused {name!r} (HTTP {response.status_code}): "
            f"{_detail(response)}"
        )


def _detail(response: httpx.Response) -> str:
    try:
        payload: object = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        fields = cast(dict[str, object], payload)
        return str(fields.get("detail", fields))
    return str(payload)
