from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import psycopg

_SPACING_DAYS = 40


class TelemetryStream:
    def __init__(self, db: psycopg.Connection) -> None:
        db.autocommit = True
        self.db = db
        slot = random.SystemRandom().randrange(1, 2000)
        self.now = datetime(2000, 1, 1, 12, tzinfo=UTC) + timedelta(days=_SPACING_DAYS * slot)
