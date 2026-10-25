# meiva:note
# Child process for the machine-spec test: a fresh interpreter binds a context that carries
# only a machine, reads `meiva.self.MACHINE_SPEC`, and reports whether anything was loaded.
# Usage: `machine_probe.py <repo root>`. The root goes on `sys.path`, so the SDK is imported
# exactly as a bare script imports it.
# meiva:link meiva/self/docs/self.meiva.okf.md

import json
import sys


def main() -> None:
    sys.path.insert(0, sys.argv[1])
    import meiva
    from base.agents.context import MeivaContext
    from base.cluster.machine.configured import ConfiguredMachineIdentity
    from base.config import ConfigBoot
    from meiva.sdk_surface import install, process_context
    from meiva.sdk_surface.installation import installed

    config = ConfigBoot()
    machine = ConfiguredMachineIdentity(config)
    machine.set(name="explicit", description="owned")
    process_context.bind_context(MeivaContext(machine=machine))
    spec = meiva.self.MACHINE_SPEC
    report = {
        "spec": list(spec),
        "config_prepared": config.prepared,
        "load_attempted": install.load_attempted(),
        "installed": installed() is not None,
        "db_stack_loaded": "psycopg" in sys.modules,
    }
    print("REPORT " + json.dumps(report))


if __name__ == "__main__":
    main()
