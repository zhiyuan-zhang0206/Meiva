# Steering pool acquisition timeout

Background steering shared the foreground TaskGroup, so an uncaught pool
acquisition timeout cancelled a healthy provider turn. We chose to handle
PoolTimeout only around reservation, before provider delivery, and reuse the
one-second stoppable poll interval. This bounds retry frequency and emits a
structured warning per failed borrow. Delivery and acknowledgement failures
keep their existing recovery semantics; broadly catching steering errors would
hide whether the provider consumed a reserved message.
