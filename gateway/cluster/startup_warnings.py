# meiva:note
# Operator warnings for a gateway whose security switches were never chosen.
#
# Two switches default to their open value: `MEIVA_GATEWAY_HUMAN_AUTH` defaults to
# `trusted_network` (a caller presenting no credential is served as the cluster
# administrator) and `MEIVA_ALERTS_WEBHOOK_TOKEN` defaults to empty (POST /api/alerts then
# trusts loopback callers). Neither default is wrong for a single box that never leaves
# loopback; both are wrong the moment the listener is reachable from another host.
#
# The gateway cannot tell "the operator chose the default" from "the operator never wrote
# the key" by reading the resolved value alone, so this reads the unit's `.env` for the key
# itself. A key that is absent and a listener bound beyond loopback is the case worth
# interrupting an operator for; changing either default is not an option (a new home has no
# password to require, and refusing to start would strand every existing deployment).

from __future__ import annotations

from pathlib import Path

__all__ = ["declared_env_keys", "startup_warnings"]

_HUMAN_AUTH_KEY = "MEIVA_GATEWAY_HUMAN_AUTH"
_ALERTS_WEBHOOK_KEY = "MEIVA_ALERTS_WEBHOOK_TOKEN"

# meiva:note
# The one bind host `serve_plan` uses for a unit that serves no other machine.
LOOPBACK = "127.0.0.1"


# meiva:note
# The keys a unit's `.env` declares, whether their value is empty or not.
#
# Deliberately a text scan: this runs before the configuration owner boots, and asking the
# settings object which keys were "set" cannot distinguish a key written as the default
# from one resolved to it.
def declared_env_keys(env_path: Path) -> frozenset[str]:
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return frozenset()
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key:
            keys.add(key)
    return frozenset(keys)


# meiva:note
# The lines to log before the listener binds, one per switch left open by omission.
#
# Empty for a loopback-only listener: both defaults are the intended single-box posture
# there, and a warning that fires on every healthy start is one nobody reads.
def startup_warnings(
    *,
    human_auth: str,
    alerts_token_configured: bool,
    env_path: Path,
    bind_host: str,
) -> list[str]:
    if bind_host == LOOPBACK:
        return []
    declared = declared_env_keys(env_path)
    warnings: list[str] = []
    if human_auth != "required" and _HUMAN_AUTH_KEY not in declared:
        # meiva:note
        # "" is uvicorn's every-interface bind; naming it as such reads as a sentence to an
        # operator, while repr("") reads as a bug in the warning.
        reach = "every interface" if bind_host == "" else bind_host
        warnings.append(
            f"{_HUMAN_AUTH_KEY} is not set in {env_path} and this gateway binds {reach}: "
            "the API is serving human callers as the cluster administrator without "
            f"authenticating them. Write {_HUMAN_AUTH_KEY}=required to {env_path} and restart "
            "the gateway to require a login."
        )
    if not alerts_token_configured:
        warnings.append(
            f"{_ALERTS_WEBHOOK_KEY} is not set in {env_path}: POST /api/alerts trusts any "
            "loopback caller without a credential, so any process on this machine can forge "
            "an alert and its IM notification, and a webhook sent from another host is "
            f"refused. Write {_ALERTS_WEBHOOK_KEY}=<secret> to {env_path} and restart the "
            "gateway."
        )
    return warnings
