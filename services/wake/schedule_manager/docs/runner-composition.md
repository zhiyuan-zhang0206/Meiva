# Single-round runner composition

Each due fire starts a gated process for one `run.py` payload. The parent records
process custody before releasing the launch gate. The runner executes the script
with `sys.argv` containing only its script path, so ordinary `argparse` scripts
receive the same inputs as a direct Python invocation. It restores the entry argv
when execution ends, including script exceptions.

Tests use real child processes for successful execution, timeout, restart recovery,
and process-group cleanup. Process-control ports supply denied-signal faults.
Durable run rows enforce single flight; recurring schedules advance only after a
run has settled.
