# meiva:note
# What the extension commands decide, as values.
#
# Each module takes the facts a command's shell read (a gateway reply, a registry row, the
# home and the default home) and returns what to print, refuse or write. The shells in
# `cli/commands/extensions` read the facts and execute what these return.
