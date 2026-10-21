# meiva:note
# Holds a home's meiva-root lock exclusively, standing in for a running meiva-root. A child
# process, not the test itself: macOS flock converts a lock the calling process already holds
# instead of reporting the conflict, so only another process can be seen as holding it.

import fcntl
import sys
from pathlib import Path

with Path(sys.argv[1]).open("rb") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    print("locked", flush=True)
    sys.stdin.read()
