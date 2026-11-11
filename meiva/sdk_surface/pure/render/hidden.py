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

# meiva:note
# The members a native-harness agent never sees rendered: `meiva.attach` binds an external
# harness process to the identity it was started as, and a native agent already has one, so the
# entry could only refuse it. The mirror of `HARNESS_HIDDEN_SELF_MEMBERS`: that set is hidden
# from external harnesses, this one from native agents, and both are applied by the same help
# profile.
NATIVE_HIDDEN_MEMBERS: frozenset[str] = frozenset({"meiva.attach"})
