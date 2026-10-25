# meiva:note
# Compose gateway access from the startup owner's explicit configuration.
# meiva:link base/cluster/machine/docs/machine.meiva.okf.md

from base.cluster.machine import GatewayAccess
from base.config import ConfigBoot

__all__ = ["gateway_access_from_boot"]


# meiva:note
# How to reach and authenticate to the gateway, from this root's configuration.
#
# Reads the configuration fields, the launch environment this owner holds and the home's
# `.env` now; a root builds it where it first dials the gateway.
def gateway_access_from_boot(config: ConfigBoot) -> GatewayAccess:
    view = config.view
    facts = config.launch_facts()
    return GatewayAccess(
        gateway_url=view.gateway.gateway_url,
        cluster_secret=view.data_plane.cluster_secret,
        remote_plane=view.data_plane.is_remote,
        machine_token=facts.machine_token,
        launcher_profile=facts.launcher_profile,
        gateway_home=facts.gateway_home,
    )
