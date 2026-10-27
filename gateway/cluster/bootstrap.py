from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from base.packages.plugins.config_registration import plugin_bootstrap_config
from gateway.http.auth.request_principal import cluster_credential
from gateway.http.model_catalog import require_model_catalog

router = APIRouter()


@router.get(
    "/api/bootstrap",
    description="Return cluster-common config ({ENV_ALIAS: value}, unmasked) for an\nagent-runner to load into its environment.\n\n`MEIVA_DB_URL` is the credential-free endpoint; no database credential is\nserved. The admin credentials remain gateway-local (see\nbase.config.bootstrap_config_values).\n\nRaises:\n    HTTPException: 401 when the request carries neither the active write\n        generation's machine API token nor the cluster secret as its bearer;\n        400 when the gateway configuration has no database\n        endpoint.",
)
def get_bootstrap(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, str]:

    secret = request.app.state.gateway_config.cluster_secret
    if (
        cluster_credential(
            authorization,
            secret,
            home=request.app.state.gateway_process.home,
            cache=request.app.state.machine_token_acceptance,
        )
        is None
    ):
        raise HTTPException(status_code=401, detail="machine API token required")
    catalog = require_model_catalog(request)
    try:
        return request.app.state.config_authority.bootstrap_config_values(
            provider_key_envs=catalog.key_envs,
            plugin_cluster_config=plugin_bootstrap_config(
                home=request.app.state.gateway_process.home
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
