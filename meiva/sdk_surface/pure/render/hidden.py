# meiva:note
# The `meiva.self` members an external harness never sees rendered: a harness runs the agent loop
# itself, so `attach`, `compact` and `restart` describe controls this agent does not own, and
# documenting them would advertise actions that cannot act. `meiva.self` re-exports the set for its
# own surface; the renderer reads it here.
HARNESS_HIDDEN_SELF_MEMBERS: frozenset[str] = frozenset(
    {
        "meiva.self.attach",
        "meiva.self.compact",
        "meiva.self.restart",
    }
)
