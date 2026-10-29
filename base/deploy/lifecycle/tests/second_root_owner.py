# meiva:note
# Claims a home's root control socket from a second process, standing in for a root that starts
# while another already owns the home. A child process, not a call in the test: the refusal is
# about two owners, and only a second process makes the first one live.

import sys
from pathlib import Path

from base.native_process.root_control.tests.status_root import serve

with serve(home=Path(sys.argv[1])):
    pass
