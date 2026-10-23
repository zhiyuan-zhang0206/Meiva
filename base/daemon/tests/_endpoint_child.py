from __future__ import annotations

import json

from base.config import get_field
from base.daemon.endpoints import ServiceEndpoints
from base.paths import meiva_home, run_dir


def main() -> None:
    table = ServiceEndpoints.from_settings()
    print(
        json.dumps(
            {
                endpoint.name: {
                    "port": endpoint.health_port,
                    "pidfile": str(endpoint.pidfile),
                    "override": get_field(f"{endpoint.name}_health_port"),
                    "legacy_pidfile": str(run_dir(meiva_home()) / f"{endpoint.name}.pid"),
                }
                for endpoint in table
            }
        )
    )


if __name__ == "__main__":
    main()
