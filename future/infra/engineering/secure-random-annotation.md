# Annotate credential generators as random.SystemRandom again

`base/lm/harness/registration.py` defines `SecureRandom = NewType("SecureRandom",
random.Random)`, and `HarnessBridge`, `BridgeRootValues` and `HostPolicy` take a
`SecureRandom` for the session handle, which is a bearer credential. The type states the
intent: only a `random.SystemRandom` built at a process root should be wrapped in it.

The wrapper exists because the scanner reads the annotation `random.SystemRandom` in
strict-to-be code as an entropy read, although a type annotation acquires nothing. When the
scanner no longer counts annotations, the three signatures return to `random.SystemRandom`
and `SecureRandom` is deleted, together with its wrapping at the roots
(`services/agent_runner/agent_host/daemon.py`, `base/lm/harness/tests/roots.py`,
`services/agent_runner/agent_host/tests/host_policy.py`).
