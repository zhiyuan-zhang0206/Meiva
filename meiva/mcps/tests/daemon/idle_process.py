# meiva:note
# A real process that does nothing for two minutes. The tests start it with the extra argv they want a process table to show (a daemon launch, a wrapper, a marker) and kill it when they are done.
import time

time.sleep(120)
