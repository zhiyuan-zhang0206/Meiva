# meiva:note
# Validate restart overlays in the real process profile selected by the parent, over the home the
# parent gave the process.

import os
import sys
from pathlib import Path

import pytest

from base.config import settings
from base.packages.plugins.config_registration import validate_config_overlay_shape
from base.packages.plugins.plugin_errors import InvalidConfigOverlay


def main(profile: str) -> None:
    assert settings.profile == profile
    assert not hasattr(settings, "agent")
    if profile == "gateway":
        assert not hasattr(settings, "sandbox")
    home = Path(os.environ["MEIVA_HOME"])
    valid: dict[str, object] = {
        "completion_notice_policy": "hourly",
        "syntax_fix_ruff_format": True,
    }
    validate_config_overlay_shape(valid, home=home)
    invalid_overlays: tuple[dict[str, object], ...] = (
        {"completion_notice_policy": "bogus"},
        {"syntax_fix_ruff_format": "not-a-bool"},
    )
    for invalid in invalid_overlays:
        with pytest.raises(InvalidConfigOverlay):
            validate_config_overlay_shape(invalid, home=home)
    sys.stdout.write(profile)


if __name__ == "__main__":
    main(sys.argv[1])
