# meiva:note
# The package door for the machine dimension: the mapping, the `unknown` sentinel and the one
# function a writer needs. Everything else stays private to the package.
from base.telemetry.machine.aliases import MACHINES
from base.telemetry.machine.identity import UNKNOWN_MACHINE, CanonicalMachine, canonical_machine

__all__ = ["MACHINES", "UNKNOWN_MACHINE", "CanonicalMachine", "canonical_machine"]
