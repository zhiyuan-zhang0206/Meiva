# meiva:note
# Reporting outside the agent loop. A schedule process has no agent identity, so
# `meiva.agents.send_message` cannot be the channel that reports a blind judge; this one needs
# nothing but a file on disk. Everything here degrades to a returned string — an alert that
# cannot be delivered must be visible in the round's output, never the round's exception.

from __future__ import annotations

import json
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

from .contracts import Settings

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


def gateway_credentials(settings: Settings):
    # meiva:note
    # The token and the address a schedule process can report through: a schedule has no agent
    # identity, so `meiva.agents.send_message` cannot be the channel that reports a blind judge,
    # while this one needs nothing but a file on disk. Address order: the run's own (`--gateway`,
    # then `WORKTREE_GC_GATEWAY`), the agent-host unit's `MEIVA_GATEWAY_URL`, then the home's
    # `.env`. An empty address means "cannot alert", never a guessed one.
    try:
        with settings.manifests.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    env = {}
    for unit in data.get("units", []):
        if unit.get("id") == "agent-host":
            env = unit.get("env", {}) or {}
            break
    address = (
        settings.gateway or env.get("MEIVA_GATEWAY_URL") or home_env(settings, "MEIVA_GATEWAY_URL")
    )
    return env.get("MEIVA_API_TOKEN") or env.get("AVA_API_TOKEN") or "", address


def post_alert(settings: Settings, content: str):
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
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status < 300, f"http {response.status}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def blind_alert(settings: Settings, root: str, expected: int, detail: str) -> str:
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
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    last = float(state.get("alerted_at") or 0)
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
        state = {"alerted_at": now, "root": root, "expected": expected, "detail": detail}
        with suppress(OSError), settings.blind_state.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
    return "alert {} ({})".format("sent" if ok else "FAILED", delivery)
