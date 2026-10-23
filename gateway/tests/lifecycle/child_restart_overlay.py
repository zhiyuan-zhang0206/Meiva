# meiva:note
# Child of `test_terminate_forward.py`: validate restart overlays in a gateway-profile process.
#
# Prints `{"valid": "accepted"|"rejected", "invalid": "accepted"|"rejected"}` as its last line.

from __future__ import annotations

import json
import sys

from base.packages.plugins.config_registration import validate_config_overlay
from base.packages.plugins.plugin_errors import InvalidConfigOverlay


def _verdict(overlay: dict[str, object]) -> str:
    try:
        validate_config_overlay(overlay, models={}, decision_models={})
    except InvalidConfigOverlay:
        return "rejected"
    return "accepted"


sys.stdout.write(
    json.dumps(
        {
            "valid": _verdict({"completion_notice_policy": "hourly"}),
            "invalid": _verdict({"completion_notice_policy": "bogus"}),
        }
    )
)
