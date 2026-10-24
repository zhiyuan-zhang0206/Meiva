# meiva:note
# What the cluster commands decide, as values.
#
# Each module takes the facts a command's shell read (a gateway reply, a probe reading, the
# home's recorded intent) and returns what to print, report or run. The shells in
# `cli/commands/cluster` read the facts and execute what these return.
