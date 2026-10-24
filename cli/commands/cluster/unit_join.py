# meiva:note
# A remote unit's join: verify its gateway, install or require its database capability.
#
# Settings-free, shared by `meiva init` (the unit's first join) and `meiva cluster db-authority
# install-unit` (a fresher capability on an initialized unit). `meiva start` does neither: a
# started unit fetches its configuration through Settings and probes its gateway itself. What
# the join refuses and which step takes which input are decided from values in
# `plans.unit_join_checks` and `plans.unit_join_steps`; this shell opens the bundle, dials the
# gateway and installs.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.host.db_authority.unit import (
    CAPABILITY_KEY_ENV,
    Bundle,
    UnitCapability,
    install_bundle,
    load_unit_capability,
    no_capability_message,
    open_bundle,
    probe_login,
)
from base.host.env.boot_policy import BootstrapFetch
from base.host.env.bootstrap import fetch_bootstrap_config
from base.host.net.url_secret import url_host
from base.host.verified_file import regular_bytes
from cli.commands.cluster.plans.unit_join_checks import (
    JoinTarget,
    data_plane_urls,
    join_target,
    require_routable_data_plane,
)
from cli.commands.cluster.plans.unit_join_steps import (
    CarriedBundle,
    installed_line,
    plan_credential,
    plan_install,
)

_MAX_BUNDLE_BYTES = 64 * 1024


# meiva:note
# A carried bundle, opened: its file and what the transport key unsealed from it.
@dataclass(frozen=True)
class _Carry:
    step: CarriedBundle
    bundle: Bundle


# meiva:note
# Open the carried bundle file under the transport key: authenticated and unexpired, or a
# refusal.
def _open(step: CarriedBundle, transport_key: str) -> _Carry:
    sealed = regular_bytes(Path(step.path), max_bytes=_MAX_BUNDLE_BYTES)
    return _Carry(step, open_bundle(sealed, transport_key))


# meiva:note
# The capability installed by a past join; a unit that holds none cannot join without a
# bundle.
def _installed_capability(home: Path) -> UnitCapability:
    held = load_unit_capability(home)
    if held is None:
        raise ValueError(no_capability_message(home))
    return held


# meiva:note
# Verify a remote unit's gateway and install or require its capability.
#
# The unit never holds the human cluster secret: it authenticates with the machine API token
# of its capability, the carried bundle's at a join and the installed one's afterwards. A
# carried bundle is opened (authenticated, unexpired) before anything is fetched; without one
# the installed capability must exist. `transport_key` is the operator's key, which the
# composition root took out of the environment; it is needed only for a bundle.
def join_gateway(
    values: dict[str, str], home: Path, capability: str | None, transport_key: str
) -> None:
    target = join_target(values, str(home), _host_of(values.get("MEIVA_GATEWAY_URL", "")))
    credential = plan_credential(
        capability, key_supplied=bool(transport_key), env_name=CAPABILITY_KEY_ENV
    )
    carry = _open(credential, transport_key) if isinstance(credential, CarriedBundle) else None
    held = carry.bundle.capability if carry is not None else _installed_capability(home)
    served = _fetch(target, held)
    require_routable_data_plane(target, [_host_of(url) for url in data_plane_urls(served)])
    if carry is not None:
        step = plan_install(carry.step, served, values)
        installed = install_bundle(
            home,
            carry.bundle,
            machine=step.machine,
            served_endpoint=step.served_endpoint,
            probe=probe_login,
        )
        Path(step.consume_path).unlink()
        print(installed_line(installed.generation.number, step.consume_path))
    # meiva:note
    # Verify connection facts without persisting a gateway-owned configuration cache.
    for key in served:
        values.pop(key, None)


# meiva:note
# The host name a URL names, empty when it names none: the plans judge host names, and reading
# them out of a URL is parsing, not deciding.
def _host_of(url: str) -> str:
    return url_host(url, fallback="")


# meiva:note
# What the gateway serves this unit at `/api/bootstrap`, fetched with the machine API token of
# the capability the join presents.
def _fetch(target: JoinTarget, held: UnitCapability) -> dict[str, str]:
    return fetch_bootstrap_config(target.gateway, bearer=held.api.token, tuning=BootstrapFetch())
