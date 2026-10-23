# meiva:note
# Child process for `test_env_bootstrap_credentials.py`: the parent hands each named
# credential the value `invented-<name>`, and this prints which of them the bootstrap saw
# (`seen`) and what became of each (`outcome`: `absent`, `replaced` or `LEAKED`). A fresh
# interpreter is the point of the exercise — the block under test runs at import, so by the
# time the parent could look, the parent's own environment is already scrubbed.
#
# Usage: `child_env_bootstrap_credentials.py <name>...`

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.fixtures import env_bootstrap

outcome = {}
for name in sys.argv[1:]:
    current = os.environ.get(name)
    if current is None:
        outcome[name] = "absent"
    elif current == "invented-" + name:
        outcome[name] = "LEAKED"
    else:
        outcome[name] = "replaced"
json.dump(
    {"seen": sorted(env_bootstrap.AMBIENT_CREDENTIAL_DIGESTS), "outcome": outcome}, sys.stdout
)
