from __future__ import annotations

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from services.supervision.roster import composition, spec


def test_plugin_gate_flows_through_annotation(*, machine_identity: MachineIdentityReader) -> None:
    from base.packages.plugins.config_registration import disk_image_path

    image = disk_image_path("meiva_memory", home=meiva_home())
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text('{"indexer_enabled": false, "lfs_enabled": false, "lfs_url": ""}\n')
    gates = composition.gate_settings()
    start = {
        s.session
        for s in spec.services_for_capabilities(
            frozenset({"gateway"}),
            settings=gates,
            machine=machine_identity,
            host=fake_host(),
            home=meiva_home(),
        )
    }
    assert "memory-indexer" not in start
    annotated = {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}),
            settings=gates,
            machine=machine_identity,
            host=fake_host(),
            home=meiva_home(),
        )
    }
    assert annotated["memory-indexer"] and "disabled" in annotated["memory-indexer"]
