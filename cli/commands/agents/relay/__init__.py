# meiva:note
# What one impersonation relay decides, as values.
#
# The relay is a loop that reads the lease's inbox, decides which rows to push, pushes them
# and reserves the delivery attempt. The shell (`impersonation_relay.py`) reads the inbox,
# reserves in the database and delivers to the host; every decision between those effects is
# a function of the snapshot it read, the clock readings it took and the tuning it was
# started with, in the modules here.
