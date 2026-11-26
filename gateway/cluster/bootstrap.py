from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from base.host.env.boot_policy import MIRROR_HOST_PARAM, issue_config_mirror
from base.packages.plugins.config_registration import plugin_bootstrap_config
from gateway.http.auth.request_principal import cluster_credential
from gateway.http.config import gateway_clock, gateway_state
from gateway.http.model_catalog import require_model_catalog

router = APIRouter()


@router.get(
    "/api/bootstrap",
    response_model=dict[str, str],
    description="Return cluster-common config ({ENV_ALIAS: value}, unmasked) for an\nagent-runner to load into its environment.\n\n`MEIVA_DB_URL` is the credential-free endpoint; no database credential is\nserved. The admin credentials remain gateway-local (see\nbase.config.bootstrap_config_values).\n\nRaises:\n    HTTPException: 401 when the request carries neither the active write\n        generation's machine API token nor the cluster secret as its bearer;\n        400 when the gateway configuration has no database\n        endpoint.",
)
def get_bootstrap(
    request: Request, authorization: str | None = Header(default=None)
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
    machine_host = request.query_params.get(MIRROR_HOST_PARAM)
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
