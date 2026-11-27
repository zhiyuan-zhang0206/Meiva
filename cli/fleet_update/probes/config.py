# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from base.cluster.machine import gateway_api_base
from base.config import ConfigBoot
from base.deploy.release.start_inputs import configuration_digest
from base.host.boundary.untyped import object_of
from base.host.env.boot_policy import (
    MIRROR_HOST_PARAM,
    BootstrapEnvelope,
    MirrorRejectedError,
    validate_config_mirror,
)
from base.host.env.bootstrap import (
    apply_bootstrap_values,
    deliver_unit_authority,
    read_config_mirror,
    stage_config_mirror,
)
from base.host.net.auth import bearer_header, delivered_token
from base.host.net.http_dial import get


def fetch_mirror(url: str, bearer: str, machine_host: str) -> object:
    response = get(
        f"{gateway_api_base(url)}/api/bootstrap",
        headers=bearer_header(bearer),
        params={MIRROR_HOST_PARAM: machine_host},
        timeout=10,
    )
    _ = response.raise_for_status()
    return object_of(response.json)


def authorize_mirror(
    env: Mapping[str, str],
    *,
    home: Path,
    now: Callable[[], float],
    stage: bool,
    minimum_lifetime: int,
    fetch: Callable[[str, str, str], object],
) -> BootstrapEnvelope:
    bearer = delivered_token(env)
    host, url = env.get("MEIVA_MACHINE_HOST", ""), env.get("MEIVA_GATEWAY_URL", "")
    if not (bearer and host and url):
        raise MirrorRejectedError("runner is missing installed API authority, host, or gateway URL")
    if stage:
        payload = fetch(url, bearer, host)
        instant = now()
        envelope = validate_config_mirror(payload, bearer=bearer, machine_host=host, now=instant)
        if envelope["expires_at"] <= instant + minimum_lifetime:
            raise MirrorRejectedError("mirror expires inside the start budget", state="expired")
        return stage_config_mirror(
            payload, home=home, bearer=bearer, machine_host=host, now=instant
        )
    installed = read_config_mirror(home=home, bearer=bearer, machine_host=host, now=now())
    if installed is None:
        raise MirrorRejectedError("no authorized mirror installed")
    return installed


def config_generation(
    config: ConfigBoot,
    env: MutableMapping[str, str],
    *,
    home: Path,
    runner: bool,
    stage: bool,
    now: Callable[[], float],
    minimum_lifetime: int,
    verify_live: bool = False,
) -> str:
    if not runner:
        _ = config.ensure_eager()
        return configuration_digest(home)
    delivery = deliver_unit_authority(env, home=home)
    if delivery.db_authority_refusal:
        raise MirrorRejectedError("installed runner authority refused")
    envelope = authorize_mirror(
        env, home=home, stage=stage, now=now, minimum_lifetime=minimum_lifetime, fetch=fetch_mirror
    )
    url = env["MEIVA_GATEWAY_URL"]
    if verify_live:
        bearer = env["MEIVA_API_TOKEN"]
        host = env["MEIVA_MACHINE_HOST"]
        live = validate_config_mirror(
            fetch_mirror(url, bearer, host), bearer=bearer, machine_host=host, now=now()
        )
        if live["generation"] != envelope["generation"]:
            raise MirrorRejectedError(
                f"live config generation {live['generation']} differs from installed "
                f"generation {envelope['generation']}"
            )
    apply_bootstrap_values(env, url, envelope["values"], home=home)
    _ = config.ensure_eager()
    return envelope["generation"]
