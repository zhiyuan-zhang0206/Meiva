"""The Service-processes block lists only gateway-only daemons.

restarter (a per-host daemon) moved to the per-machine roster; labeler +
memory_indexer stay here as the gateway-only set."""

import gateway.cluster.status as status_mod


def test_services_status_is_gateway_only():
    svc = status_mod.get_services_status(pidfile_checker=lambda _p: (True, 4242))
    names = {item.name for item in svc.items}
    assert names == {"labeler", "memory_indexer"}
    assert "restarter" not in names  # per-host now -> roster, not here
