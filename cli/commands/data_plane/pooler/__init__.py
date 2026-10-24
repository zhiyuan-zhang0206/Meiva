# meiva:note
# What the pooler's bring-up and stop decide, as values.
#
# Each module takes the facts the shell read (the files on disk, the process table, the
# listener table, the clock reading) and returns the configuration text, the steps to run,
# the line to print or the refusal. `cli/commands/data_plane/pgbouncer.py` and
# `_pooler_stop.py` execute what these return.
