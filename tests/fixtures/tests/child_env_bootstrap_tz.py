# meiva:note
# Child probe for test_env_bootstrap_tz.py. A fresh interpreter is required because
# env_bootstrap runs at import time.

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

incoming_tz = os.environ.get("TZ")

from tests.fixtures import env_bootstrap  # noqa: E402

_ = env_bootstrap

json.dump(
    {
        "incoming_tz": incoming_tz,
        "bootstrapped_tz": os.environ.get("TZ"),
        "timezone": getattr(time, "timezone", 0),
        "tzname": list(getattr(time, "tzname", ())),
    },
    sys.stdout,
)
