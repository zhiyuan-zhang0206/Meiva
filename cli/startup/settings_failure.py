# meiva:note
# How a Settings failure on an unconfigured home reads.

from __future__ import annotations

from pydantic import ValidationError

__all__ = ["settings_failure_text"]


# meiva:note
# The text to print for a Pydantic Settings ValidationError: a copy-paste env template.
#
# Hit on a fresh host (no ~/.meiva/.env, or missing required fields like
# MEIVA_DB_URL / MEIVA_REDIS_URL). `meiva start` resolves these; this words the
# minimum env template for the manual path.
def settings_failure_text(e: ValidationError) -> str:
    missing: list[str] = []
    for err in e.errors():
        loc = err.get("loc", ())
        if not loc:
            continue
        token = loc[0]
        if isinstance(token, str):
            # meiva:note
            # Settings fields all use explicit aliases like `alias="MEIVA_X"`,
            # but Pydantic's error loc reports the Python attribute name.
            # Convert by uppercasing + MEIVA_ prefix; matches every alias today.
            missing.append(token if token.startswith("MEIVA_") else f"MEIVA_{token.upper()}")
    lines = ["\n✗ meiva: failed to load settings — ~/.meiva/.env is missing required fields.\n"]
    if missing:
        lines.append("Missing env vars:")
        lines.extend(f"  {var}=<value>" for var in missing)
    lines.append(
        "\nAdd the lines above to ~/.meiva/.env, then re-run your command. For the full\n"
        "agent-runner bring-up flow, use `meiva init --serve-agent-runner --no-serve-gateway --gateway-url <url> --machine-name <name> "
        "--machine-host <this-host-addr> --db-capability <bundle>`, then `meiva start` with this unit's explicit service selection."
    )
    return "\n".join(lines)
