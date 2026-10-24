# meiva:note
# A child process that runs the root's Redis ACL diagnostic under the settings its parent chose
# and prints the verdict and detail as JSON. The diagnostic reads the Redis endpoint from process
# settings, so each endpoint needs a process of its own.

from __future__ import annotations

import asyncio
import json
import sys

from services.supervision.meiva_root.health import ProbeRunner
from services.supervision.meiva_root_glue import diagnostic_probes as probes


async def main() -> None:
    result = await ProbeRunner().observe(probes.redis_acl, 5)
    sys.stdout.write(json.dumps({"verdict": result.verdict.value, "detail": result.detail}) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
