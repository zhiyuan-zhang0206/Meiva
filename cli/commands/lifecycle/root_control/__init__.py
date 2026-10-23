# meiva:note
# What the lifecycle commands decide about the application root, as values.
#
# `snapshot` parses the root's status reply into a typed value, `reconcile_plan` decides
# what to resume, refuse or stop from that value, `poll_plan` decides the next step of
# every wait on the root or its keeper, and `launch_plan` builds the command line, seed and
# launch inputs of a root launch. `generation` binds a launch to its source and
# configuration. The shell that talks to the root (`root_driver`, `root_readiness`) reads
# the clock, the socket and the processes, and executes what these modules return.
