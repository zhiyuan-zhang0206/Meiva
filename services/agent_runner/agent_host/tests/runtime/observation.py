from dataclasses import dataclass

from base.cluster.machine import MachineIdentityReader


# meiva:note
# The model id a built chat model serves, whichever provider client it is: the clients name it
# `model` or `model_name`.
def served_model(llm: object) -> str:
    for attribute in ("model", "model_name"):
        value = getattr(llm, attribute, None)
        if isinstance(value, str):
            return value
    raise AssertionError(f"{type(llm).__name__} names no model")


# meiva:note
# What one invocation saw of its context, recorded from inside the graph: the agent, the model
# the slices resolved and the model the runtime was built for, the agent's plugin config, whether
# its event publisher's worker ran, and the pool and machine the turn was handed.
@dataclass
class HostTurnObservation:
    agent_id: int | None
    model: str
    plugin_marker: str
    served_model: str
    publisher_running: bool
    ops_pool: object
    machine: MachineIdentityReader
