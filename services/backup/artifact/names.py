# meiva:note
# Naming grammar of the daily logical dump pool.
#
# One source of truth for a managed dump's file name, shared by the local
# due-ness and prune logic (`services.backup.dump`) and the scheduled commit
# (`services.backup.scheduler.worker`). The pool is flat:
# ``<db>-<UTC stamp>.dump.enc`` (a bare ``.dump`` stays inside the grammar). A
# file outside it is never counted or pruned as a managed dump: a
# hand-made dump parked beside them is left alone.

from __future__ import annotations

import re
from datetime import UTC, datetime

DUMP_NAME_RE = re.compile(r"^(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z)\.dump(?:\.enc)?$")
TS_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, offset-bearing by construction


# meiva:note
# A name's stamp token as an aware UTC instant.
#
# The token is UTC by construction (its ``Z`` is part of the grammar), so ordering stamps
# orders real instants whatever the host's or cluster's clock.
def stamp_utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, TS_FORMAT).replace(tzinfo=UTC)
