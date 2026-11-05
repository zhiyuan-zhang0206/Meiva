# Explicit command inputs and log timestamps

The repayment pass uses the terminal already captured by `CommandRuntime` for
stop confirmation and harness input. Re-reading a process stream inside a command
would let embedded invocations observe a terminal other than the supplied one.

Capability-expiry reports use the invocation's configured timezone, or UTC when
none was configured. The previous implicit host timezone let identical bundles
print differently on different operator machines. F12 requests UTC timestamps
from macOS `log show` and passes that zone into its parser; capturing the host's
current UTC offset would misread a window spanning a daylight-saving transition.

Execution plans retain program names for the subprocess launcher to resolve.
Resolving `pnpm` and `npx` at module import freezes the planning process's PATH,
while execution owns its environment. The optional GitHub read already handles
`OSError`, so a separate `which` preflight adds no failure behavior.

No root declarations or rule allowances change in this batch. The SDK
interpreter fact remains the separate prerequisite tracked by task #6128.

## Review update

The UTC fallback proposed above was withdrawn during review. Capability reports
use the timezone from the invocation's configuration view, including the existing
`America/Los_Angeles` field default when no override was supplied. Debt repayment
does not replace that established configuration default with a new one. F12's UTC
choice is different: the log producer is explicitly requested to emit UTC.
