from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from cli.startup.service_path import BirthFacts

__all__ = [
    "BIRTH_NOW",
    "MINTED_BACKUP_PASSPHRASE",
    "MINTED_CLUSTER_SECRET",
    "MINTED_REDIS_ADMIN_PASSWORD",
    "MINTED_REDIS_PASSWORD",
    "birth_facts",
]

BIRTH_NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


# meiva:note
# A fixed 64-character hexadecimal value per label, so each minted secret of a test is distinct and
# recognizable without a credential-shaped literal.
def _minted(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


MINTED_CLUSTER_SECRET = _minted("cluster")
MINTED_REDIS_ADMIN_PASSWORD = _minted("redis-admin")
MINTED_REDIS_PASSWORD = _minted("redis")
MINTED_BACKUP_PASSPHRASE = _minted("backup-passphrase")


# meiva:note
# Fixed birth facts: a test reads the instant and the minted secrets a first start recorded.
def birth_facts() -> BirthFacts:
    return BirthFacts(
        now=BIRTH_NOW,
        cluster_secret=MINTED_CLUSTER_SECRET,
        redis_admin_password=MINTED_REDIS_ADMIN_PASSWORD,
        redis_password=MINTED_REDIS_PASSWORD,
        backup_passphrase=MINTED_BACKUP_PASSPHRASE,
    )
