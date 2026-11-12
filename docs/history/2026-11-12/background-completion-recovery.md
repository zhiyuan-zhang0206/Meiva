# Background completion recovery

Task #6552 keeps the command exit status independent of completion delivery.
A gateway restart can otherwise leave a successfully finished command with no
wake-up for its waiter. The approved recovery owner is the cluster operator,
using the existing gateway health-check chain rather than another daemon.

The sender records intent before dialing and shares one client-message key
with retries. Recording only after a failed request would leave a crash window
between failure and persistence, while minting a key on replay would duplicate
an ambiguously accepted send. Pending intents protect their output logs from
normal ring eviction.

Recovery is a bounded local pass. Runtime integration is staged separately for
the existing macmini gateway-recovery watcher, including initial healthy polls;
a recovery-edge-only hook would miss receipts left before that watcher started.
No fleet rollout, production outage or cross-host SSH polling is part of this
code change. Other hosts require their own approved existing hook before
claiming automatic recovery coverage.
