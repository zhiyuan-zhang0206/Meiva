# meiva:note
# What a fleet-update window decides, as values.
#
# Each module takes the facts the shell read (a host's probe output, a reply from a program the
# window shipped, the operator's choices) and returns what to run, say or refuse. The shell in
# `cli.fleet_update` reads the facts, executes the commands and prints the lines these return.
# Like the shell, nothing here imports application code, so a window runs from a bare checkout.
