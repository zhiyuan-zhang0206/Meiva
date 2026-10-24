import sys
from pathlib import Path

from base.native_process.os_platform import LockTimeoutError
from services.backup.dump import backup_lock
from services.backup.layout import BackupTree

if __name__ == "__main__":
    try:
        with backup_lock(BackupTree(Path(sys.argv[1])), timeout_s=0):
            pass
    except LockTimeoutError:
        raise SystemExit(7) from None
