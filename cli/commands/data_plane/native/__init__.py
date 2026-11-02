# meiva:note
# The shells of a home's own native processes, and the bounds they start under.
#
# Redis's shell (`redis_instance`, of `instance/redis_plan.py`) and the bounds every native start
# waits under (`start_bounds`: `StartTuning`, the reachable-address wait) live here together because
# both halves of the pair read them and neither half owns them. The Postgres shell and the ordered
# start of the pair stay one level up, in `cluster_instance`.
