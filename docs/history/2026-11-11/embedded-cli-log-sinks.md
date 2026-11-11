# Embedded CLI log sink ownership

CI run 42872750240 reported a closed stderr stream in Loguru handler 1 after an
embedded CLI invocation. Command shutdown drained its event writer while leaving
process logging's stderr, file and event sinks registered. The next warning then
wrote to the prior capture stream.

The command owner now retains the cleanup returned by process logging startup.
Closing the command releases its installed sinks and permits the next invocation
to initialize logging again. Initialization failure also releases opened sinks.
Already initialized daemon logging remains owned by that daemon. The regression
runs two command lifetimes, closes each stderr stream, and logs afterward while
retaining a caller sink; it covers normal and failed command bodies.
