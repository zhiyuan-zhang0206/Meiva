# meiva:note
# Validate restart overlays in the real process profile selected by the parent.

import sys

import pytest
from pydantic import ValidationError

from base.config import settings
from ops.rpc_schemas import RestartAgentRequest


def main(profile: str) -> None:
    assert settings.profile == profile
    assert not hasattr(settings, "agent")
    if profile == "gateway":
        assert not hasattr(settings, "sandbox")
    valid: dict[str, object] = {
        "completion_notice_policy": "hourly",
        "syntax_fix_ruff_format": True,
    }
    assert RestartAgentRequest(config_overlay=valid).config_overlay == valid
    invalid_overlays: tuple[dict[str, object], ...] = (
        {"completion_notice_policy": "bogus"},
        {"syntax_fix_ruff_format": "not-a-bool"},
    )
    for invalid in invalid_overlays:
        with pytest.raises(ValidationError):
            RestartAgentRequest(config_overlay=invalid)
    sys.stdout.write(profile)


if __name__ == "__main__":
    main(sys.argv[1])
