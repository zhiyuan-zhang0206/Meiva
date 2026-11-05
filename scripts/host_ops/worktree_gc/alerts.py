# meiva:note
# Reporting outside the agent loop. A schedule process has no agent identity, so
# `meiva.agents.send_message` cannot be the channel that reports a blind judge; this one needs
# nothing but a file on disk. Everything here degrades to a returned string — an alert that
# cannot be delivered must be visible in the round's output, never the round's exception.

from __future__ import annotations

import http.client
import json
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

from .contracts import Settings
from .values import float_of, object_dict, text_of

# meiva:note
# One alert per blind episode per window, never one per round: the condition is not the alert,
# and a round is every five minutes.
BLIND_ALERT_HOURS = 6


def home_env(settings: Settings, key: str) -> str:
    # meiva:note
    # The value in the home's own `.env` — the file the runtime reads, so an address configured
    # once is not configured a second time here. Missing file or key reads as empty.
    try:
        with Path(settings.home_env).open(encoding="utf-8") as handle:
            rows = handle.read().splitlines()
    except OSError:
        return ""
    for row in rows:
        name, sep, value = row.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def agent_host_env(settings: Settings) -> dict[str, object]:
    # meiva:note
    # The `agent-host` unit's environment block, read as `object` values and narrowed one lookup
    # at a time: `manifests.json` is written by another component, so its shape is a claim to
    # check, not a type to trust.
    try:
        with settings.manifests.open(encoding="utf-8") as handle:
            data = object_of(json.load, handle)
    except (OSError, ValueError):
        return {}
    if not is_object_dict(data):
        return {}
    units = data.get("units")
    if not is_object_list(units):
        return {}
    for unit in units:
        if is_object_dict(unit) and unit.get("id") == "agent-host":
            env = unit.get("env")
            return dict(env) if is_object_dict(env) else {}
    return {}


def gateway_credentials(settings: Settings) -> tuple[str, str | None]:
    # meiva:note
    # The token and the address a schedule process can report through: a schedule has no agent
    # identity, so `meiva.agents.send_message` cannot be the channel that reports a blind judge,
    # while this one needs nothing but a file on disk. Address order: the run's own (`--gateway`,
    # then `WORKTREE_GC_GATEWAY`), the agent-host unit's `MEIVA_GATEWAY_URL`, then the home's
    # `.env`. An empty address means "cannot alert", never a guessed one.
    env = agent_host_env(settings)
    address = (
        settings.gateway
        or text_of(env.get("MEIVA_GATEWAY_URL"))
        or home_env(settings, "MEIVA_GATEWAY_URL")
    )
    token = text_of(env.get("MEIVA_API_TOKEN")) or text_of(env.get("AVA_API_TOKEN"))
    return token, address or None


def post_alert(settings: Settings, content: str) -> tuple[bool, str]:
    # meiva:note
    # Returns `(delivered, detail)` and never raises: the caller prints the detail, so a failed
    # alert is visible in the round's output instead of being the round's exception.
    token, url = gateway_credentials(settings)
    if not token:
        return False, "no token in manifests.json"
    if not url:
        return False, "no gateway address configured"
    body = json.dumps({"content": content, "source": "external_agent:worktree-gc"}).encode()
    request = urllib.request.Request(  # noqa: S310
        f"{url.rstrip('/')}/api/agents/{settings.owner}/messages",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        handle = object_of(urllib.request.urlopen, request, timeout=20)
        if not isinstance(handle, http.client.HTTPResponse):
            return False, f"unexpected handle {type(handle).__name__}"
        with handle:
            return handle.status < 300, f"http {handle.status}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def blind_alert(settings: Settings, root: str | Path, expected: int, detail: str) -> str:
    # meiva:note
    # Say once per window that the snapshot judge is blind — silence is the bug.
    #
    # Emptiness is the one failure this tool cannot see on its own: a probe that reads
    # nothing prints the same thing as a repo with nothing to reclaim. So a blind round
    # reports to the owner and deletes nothing; the alert is rate-limited, the condition
    # is not.
    now = time.time()
    try:
        with settings.blind_state.open(encoding="utf-8") as handle:
            state = object_dict(object_of(json.load, handle))
    except (OSError, ValueError):
        state = {}
    last = float_of(state.get("alerted_at"))
    if now - last < BLIND_ALERT_HOURS * 3600:
        return f"already alerted {(now - last) / 3600.0:.1f}h ago"
    ok, delivery = post_alert(
        settings,
        f"[worktree-gc] the snapshot judge is blind: {root} should hold {expected} tree(s) "
        f"and the probe gave {detail.replace('probe listed ', '').replace(' trees', '')}. "
        "This round reports and deletes nothing; the previous alert was "
        f"{(f'{(now - last) / 3600.0:.1f} h ago') if last else 'none'}.",
    )
    if ok:
        state = {
            "alerted_at": now,
            "root": str(root),
            "expected": expected,
            "detail": detail,
        }
        with suppress(OSError), settings.blind_state.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
    return "alert {} ({})".format("sent" if ok else "FAILED", delivery)
