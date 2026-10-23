# meiva:note
# A known process group whose members ignore graceful PTY closure.
# meiva:link base/sessions/pty/docs/pty_sessions/closure-inputs.meiva.okf.md

import os
import signal
import sys
import time

signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = os.fork()
if child:
    sys.stdout.write(f"{child}\n")
    sys.stdout.flush()
time.sleep(300)
