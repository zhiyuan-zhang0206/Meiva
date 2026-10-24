from enum import Enum

__all__ = ["SurvivorRole"]


# meiva:note
# What a known process the closure reported is to its session: the shell itself or any other
# recorded member. These are the two words the outcome carries on the wire; any other word is a
# contract break between the service and its reader, refused by `SurvivorRole(raw)` with a
# `ValueError` where the outcome is read, never taken for a job.
class SurvivorRole(Enum):
    SHELL = "shell"
    JOB = "job"
