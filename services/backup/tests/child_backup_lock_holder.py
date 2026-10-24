import sys
import time
from pathlib import Path

from services.backup.dump import backup_lock
from services.backup.layout import BackupTree

if __name__ == "__main__":
    with backup_lock(BackupTree(Path(sys.argv[3])), timeout_s=60):
        Path(sys.argv[1]).write_text("1", encoding="utf-8")
        time.sleep(float(sys.argv[2]))
