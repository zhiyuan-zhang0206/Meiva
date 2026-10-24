# meiva:note
# Where the logical-backup tree lives, as one value.
#
# Every backup path (the managed dump pool, the lock that serializes writers, the
# scheduled workers' private controls and the weekly drill's success marker) hangs off
# one directory, `<home>/backups`. A composition root builds the value once from
# its home; every function below it receives the value, so none of them reads the
# home itself and a test hands them a home of its own.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["BackupTree"]


# meiva:note
# The backup tree of one home.
@dataclass(frozen=True)
class BackupTree:
    home: Path

    # meiva:note
    # `<home>/backups`, which holds everything below.
    @property
    def root(self) -> Path:
        return self.home / "backups"

    # meiva:note
    # The flat pool of managed dumps; the home already scopes the cluster, so the
    # directory carries no per-cluster token.
    @property
    def managed(self) -> Path:
        return self.root / "db"

    # meiva:note
    # The file lock that serializes backup creation, sweeps and pruning.
    @property
    def lock(self) -> Path:
        return self.root / ".db-backup.lock"

    # meiva:note
    # The private controls of one scheduled operation kind.
    def controls(self, operation: str) -> Path:
        return self.root / "operations" / operation

    # meiva:note
    # The marker the weekly logical restore drill publishes after a success.
    @property
    def restore_marker(self) -> Path:
        return self.managed / ".logical-restore-drill.json"
