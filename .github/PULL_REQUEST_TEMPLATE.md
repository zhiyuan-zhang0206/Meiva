<!-- Squash is the only merge method, so `main` receives this description as the commit
message and the agent attribution below survives only when it is written here. -->

<!-- Follow docs/contributing.md. Keep detail proportional to the change;
remove sections that do not help assess it. -->

## What and why

<!-- Explain the concrete problem and resulting behavior. Add a file tree or
control-flow explanation when it helps readers understand a larger change. -->

## Validation

<!-- Record relevant checks and material gaps. Distinguish successful execution
from skipped, missing or incomplete verification. -->

## Tech-debt registration

<!-- A PR that knowingly introduces debt — a deliberate shortcut, deferred work or
TODO, a suppression, or a patch over a private dependency API — adds or extends one
`### <fingerprint>` entry in `future/tech-debt/ledger.md`, using the fingerprint
convention that file documents (`<class>:<file>:<symbol>`, or `<class>:<slug>` for a
finding that spans files). Write `none` when the PR introduces no such debt. There is
no CI gate; the ledger's reconcile pass reads what this section records. -->

## Attribution

<!-- The id of the Meiva agent that did the work. A change a person made without an agent
(`meiva-*` is also the owner's own branch prefix) replaces the line below with the exit
line that the attribution-gate workflow spells out, `Meiva: none`, alone on a line. -->

Co-authored-by: Meiva #<id>
