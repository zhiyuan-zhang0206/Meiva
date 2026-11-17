# Retire schedule daemon compatibility

Schedules now use declarative `at`, `cron`, or `every` triggers and one bounded
process per fire. Resident PTY compatibility duplicated lifecycle and recovery
policy after declarative scheduling became the public interface, so it was
removed with its revision convergence, restart, and stall machinery.

The schema migration refuses remaining daemon rows, requiring their conversion
before deployment. The inbound-message watcher source vocabulary remains intact.
The split-unit election still gates reconciliation because database fire claims
alone do not elect which local home owns running child processes.

The runner presents only the payload path in `sys.argv` and restores its original
value on exit; this preserves direct-script `argparse` behavior. Historical schedule
event declarations remain available for interpreting previously recorded telemetry.
