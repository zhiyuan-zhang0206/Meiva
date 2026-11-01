# meiva:note
# A real process that does nothing for two minutes. Callers that need `execve` to finish populating
# `/proc/<pid>/cmdline` before inspecting it pass `--ready-on-stdout`; callers that need a silent
# stdio peer (`test_connect.py`) omit the flag. The tests append any extra argv they want a process
# table to show and kill the process when they are done.
import sys
import time

if "--ready-on-stdout" in sys.argv[1:2]:
    sys.stdout.write("ready\n")
    sys.stdout.flush()
time.sleep(120)
