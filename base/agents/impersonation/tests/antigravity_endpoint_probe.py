from __future__ import annotations

import json
import sys

from base.agents.impersonation.host_transport import antigravity_rpc, resolve_antigravity_endpoint


# meiva:note
# Run by ``test_host_transport`` in a child process with a projected environment: the
# accepting runtime's situation, where no ``ANTIGRAVITY_*`` variable exists and the
# endpoint can only come from the record the request wrote. The RPC is read-only.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def main() -> int:
    endpoint = resolve_antigravity_endpoint(sys.argv[1])
    sys.stdout.write(json.dumps(antigravity_rpc("GetWorkspaceInfos", {}, server=endpoint)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
