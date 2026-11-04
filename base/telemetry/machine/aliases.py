# meiva:note
# The four hostnames recorded in `audit_events.machine`, and the machine each one is.
#
# Measured, not guessed: every entry was verified on 2026-11-04 by joining
# `attributes.spawn.ssh_connection`'s destination address to the `machines` table, and confirmed
# by the `caller.cwd` user of the same rows; each hostname matched exactly one machine across
# 16/16 rows. Two of them look like the wrong machine: `zhangzhiyuandeMac-mini.local` is
# company-mini and `zhiyuanzhangs-MacBook-Air.local` is company-air, so a mapping derived from the
# hostname string would mis-book both.
#
# `EVIDENCE` carries what was checked; `test_identity.py` refuses an entry without it.
#
# A value outside this mapping is passed through unchanged, and `unknown` is never mapped.
# meiva:link docs/conventions/testing.md

from __future__ import annotations

__all__ = ["EVIDENCE", "MACHINES"]

MACHINES: dict[str, str] = {
    "Zyons-Mac-mini.local": "macmini",
    "zhangzhiyuandeMac-mini.local": "company-mini",
    "Zhiyuans-MacBook-Air-606.local": "macbook-air",
    "zhiyuanzhangs-MacBook-Air.local": "company-air",
}

EVIDENCE: dict[str, dict[str, object]] = {
    "Zyons-Mac-mini.local": {
        "machine": "macmini",
        "caller_user": "zyonzhang",
        "rows": 489,
        "source": "spawn.ssh_connection destination joined to machines.gateway_url",
        "verified_at": "2026-11-04T18:03:00+08:00",
    },
    "zhangzhiyuandeMac-mini.local": {
        "machine": "company-mini",
        "caller_user": "zhiyuan-output",
        "rows": 60,
        "source": "spawn.ssh_connection destination joined to machines.gateway_url",
        "verified_at": "2026-11-04T18:03:00+08:00",
    },
    "Zhiyuans-MacBook-Air-606.local": {
        "machine": "macbook-air",
        "caller_user": "zhiyuanzhang",
        "rows": 129,
        "source": "spawn.ssh_connection destination joined to machines.gateway_url",
        "verified_at": "2026-11-04T18:03:00+08:00",
    },
    "zhiyuanzhangs-MacBook-Air.local": {
        "machine": "company-air",
        "caller_user": "zhiyuanzhang",
        "rows": 63,
        "source": "spawn.ssh_connection destination joined to machines.gateway_url",
        "verified_at": "2026-11-04T18:03:00+08:00",
    },
}
