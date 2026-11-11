# meiva:note
# The `meiva.self` members an external harness never sees rendered: a harness runs the agent loop
# itself, so `attach` and `compact` describe controls this agent does not own, and documenting
# them would advertise actions that cannot act. `restart` is rendered: on an external harness it
# is recorded and applied by the host when the turn ends. `meiva.self` re-exports the set for its
# own surface; the renderer reads it here.
HARNESS_HIDDEN_SELF_MEMBERS: frozenset[str] = frozenset(
    {
        "meiva.self.attach",
        "meiva.self.compact",
    }
)
