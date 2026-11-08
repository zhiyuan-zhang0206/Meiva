# meiva:note
# A stand-in for `merge_gate.py`, for the shell's tests only: it records that it ran (once, from
# which directory), prints what a gate prints, and exits with the code the test asked for. The
# shell's contract is about what a wrapper does to the module it forwards -- how many times it
# runs, what reaches stdout, which code comes back -- and none of that can be observed without
# something to forward to.
import os
import sys
from pathlib import Path

with Path(os.environ["MERGE_READY_STUB_COUNTER"]).open("a") as calls:
    calls.write(str(Path.cwd()) + "\n")
print("stub-stdout " + " ".join(sys.argv[1:]))
print("stub-stderr", file=sys.stderr)
sys.exit(int(os.environ.get("MERGE_READY_STUB_RC", "0")))
