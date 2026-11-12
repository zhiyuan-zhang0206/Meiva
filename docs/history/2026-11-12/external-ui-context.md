# Resolve external UI context at the SDK entry

Persistent UI delivery now needs identity and a runtime operation key. Reading
only the bound slot made a first UI call fail in a valid bridge-launched shell,
while an agents or tasks call first attached that same process successfully.
UI operations therefore use the existing lazy context entry, then validate that
resolved caller. They retain AskBatch validation, ordered IDs and runtime retry
keys. Host posture and attachment admission remain owned by the context entry.

The CLI also advertised an environment default without passing it to attachment.
Its adapter now reads the bridge handle from the invocation's captured environment;
a missing or blank value fails before opening an attachment or executing input.
An agent id is an assertion against a registration, never an authorization source.
No global identity fallback, public attach alias or launcher mutation is needed.

Tests use controlled HTTP for native/external UI semantics and fresh children with
invalid registrations for first-call refusal. They create no production questions.
The earlier attachment probe had pointed one directory below the repository root;
correcting it and checking the UI module path ensures children test this source.
