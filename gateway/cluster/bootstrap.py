from __future__ import annotations

from dataclasses import make_dataclass
from typing import Annotated, get_type_hints

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from base.host.boundary.untyped import object_mapping
from base.host.env.boot_policy import MIRROR_HOST_PARAM, BootstrapEnvelope, issue_config_mirror
from base.packages.plugins.config_registration import plugin_bootstrap_config
from gateway.http.auth.request_principal import cluster_credential
from gateway.http.config import gateway_clock, gateway_state
from gateway.http.model_catalog import require_model_catalog

router = APIRouter()


@router.get(
    "/api/bootstrap",
    response_model=dict[str, str]
    | make_dataclass(
        "BootstrapEnvelope", object_mapping(get_type_hints(BootstrapEnvelope)).items()
    ),
    responses={
        401: {"description": "A valid cluster bearer is required."},
        403: {"description": "Signed mirror issuance requires a runner API token."},
    },
    description=(
        "Return unmasked cluster configuration for enrollment and runner boot. "
        "Requests without mirror_machine_host receive a flat map of environment aliases. "
        "A runner API token with mirror_machine_host receives a signed, host-bound "
        "configuration envelope with an expiration time. MEIVA_DB_URL is the "
        "credential-free endpoint in both representations."
    ),
)
def get_bootstrap(
    request: Request,
    authorization: str | None = Header(default=None),
    machine_host: Annotated[str | None, Query(alias=MIRROR_HOST_PARAM)] = None,
) -> dict[str, str] | JSONResponse:
    state = gateway_state(request)
    home = state.gateway_process.home
    credential = cluster_credential(
        authorization,
        state.gateway_config.cluster_secret,
        home=home,
        cache=state.machine_token_acceptance,
    )
    if credential is None:
        raise HTTPException(status_code=401, detail="machine API token required")
    if machine_host is not None and (credential != "machine_token:runner" or authorization is None):
        raise HTTPException(status_code=403, detail="mirror issuance requires a runner API token")
    catalog = require_model_catalog(request)
    try:
        values = state.config_authority.bootstrap_config_values(
            provider_key_envs=catalog.key_envs,
            plugin_cluster_config=plugin_bootstrap_config(home=home),
        )
        if machine_host is None or authorization is None:
            return values
        envelope = issue_config_mirror(
            values,
            bearer=authorization.removeprefix("Bearer "),
            machine_host=machine_host,
            now=gateway_clock(request).now().timestamp(),
        )
        return JSONResponse(envelope, headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
