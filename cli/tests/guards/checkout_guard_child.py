# meiva:note
# A private process reads its own home and runs the real pre-Settings checkout gate.

import json
import sys
from pathlib import Path

from base.paths.home import resolve_meiva_home
from cli.preflight import require_own_checkout

if __name__ == "__main__":
    decision = require_own_checkout(sys.argv[2:], Path(sys.argv[1]))
    assert "base.config" not in sys.modules, "the checkout gate must remain Settings-free"
    print(json.dumps({"home": str(resolve_meiva_home()), "decision": decision}))
