# Schedule verification uses the invocation interpreter

`CommandRuntime.host.platform` already carries the interpreter selected at
composition. The schedule-verification child now receives that executable through
the verification pipeline instead of reading process globals in the command.
This keeps embedded command invocations on their supplied runtime and makes an
unavailable declared interpreter a tool error rather than an ambient fallback.
The SDK process-fact prerequisite in task #6128 remains separate: this CLI path
already had the value it needed.

## Sampling input from the same invocation

The sampling factory likewise receives the live environment mapping its existing
composition roots already hold. ConfigBoot supplies the attached owner's mapping
or the live process environment; the bare SDK bootstrap supplies its own process
environment. Keeping the mapping live preserves subsequent credential/flag
delivery, rather than freezing a second copy inside the factory.

Provider-key construction receives the same configuration-owned environment from
its existing SDK, agent-host, labeler and verification roots. The existing
missing-key fallback to the home's dotenv file stays in place, including its
failure when that file is malformed; a complete live key set still bypasses it.

The provider plugin suite was already at its file budget; the longer explicit
key-input calls crossed it. Its key-validation scenario now lives beside the
existing key-fallback scenarios, sharing that acquisition/validation boundary,
rather than splitting the plugin registry scenarios into numbered fragments.
