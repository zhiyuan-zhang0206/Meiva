# meiva:note
# Antigravity reconciliation: decide from the CLI's own durable message queue whether an input reached
# the model (`judge.py`, `payload.py`, pure) and read that queue from an injected user home
# (`queue_files.py`, effect). The queue is `brain/<conversation>/.system_generated/messages/`, not the
# transcript: a message is written there when `SendUserCascadeMessage` answers 200.
