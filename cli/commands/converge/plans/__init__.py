# meiva:note
# What the converge steps decide, as values.
#
# Each module takes the facts a step's shell read (paths, platform, the current state of the
# firewall, the checkout) and returns what to run, skip, warn about or print. The steps in
# `cli/commands/converge` and `cli/commands/lifecycle/converge_host.py` read the facts and
# execute what these return.
