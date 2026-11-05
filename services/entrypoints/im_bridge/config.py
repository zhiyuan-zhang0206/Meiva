"""Configuration slices of the IM Bridge daemon.

Each slice holds exactly the fields one part of the bridge reads, under the flat
field name the registry, `.env` and the config API already use. The slices are
plain frozen dataclasses: `services/entrypoints/im_bridge/daemon.py` (the composition root)
is the only module that reads `settings` and builds them, and everything else
receives the slice it needs through its constructor. See
`future/infra/security/dependency-injection.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# meiva:note
# The directory of the bridge's disk-backed state (switch state, Weixin account and caches) under a Meiva home. Each process root passes it
# on; nothing below the roots reads the home.
def state_dir_of(home: Path) -> Path:
    return home / "state" / "im_bridge"


@dataclass(frozen=True)
class ImBridgeConfig:
    """What the bridge core, its gateway client, push watchdog, push health and notice bridge read."""

    im_disabled_adapters: tuple[str, ...]
    im_send_retry_delays: tuple[float, ...]
    im_push_retry_backoff_seconds: float
    im_push_retry_jitter_seconds: float
    im_sse_read_timeout_seconds: float
    im_bridge_timeline_window: int
    im_bridge_replay_messages: int
    im_bridge_notice_reply_window_seconds: int
    im_bridge_notice_open_limit: int
    # The gateway's own display default, shared with `GET /api/notices/live`.
    im_push_health_channels: tuple[str, ...]
    im_push_health_interval_seconds: float
