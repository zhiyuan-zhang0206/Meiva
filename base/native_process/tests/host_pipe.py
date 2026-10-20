# meiva:note
# The control pipe a live host holds for an exec child, for tests that spawn the child themselves.

import os
from collections.abc import Generator
from contextlib import contextmanager


# meiva:note
# The read end to pass as the child's ``stdin``; the write end stays open for the block.
#
# An exec child kills its own process group when its stdin reaches end-of-file, so a
# test that starts `agent.execution.child` outside `run_in_subprocess` holds this open
# until the child has exited, exactly as the host keeps its end until the reap.
@contextmanager
def live_host_control() -> Generator[int]:
    read_end, write_end = os.pipe()
    try:
        yield read_end
    finally:
        os.close(write_end)
        os.close(read_end)
