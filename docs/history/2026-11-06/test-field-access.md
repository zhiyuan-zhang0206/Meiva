# Preserve discovery coverage while repaying finite test-field access

The inventory assigned the same exit description to finite test cases and
open-ended discovery. This pass repairs the finite cases while retaining debts
whose tests must discover newly added modules, schema fields or registrations.

Fixed SDK members are named explicitly. Whole-record assertions use dataclass
snapshots or raw Pydantic field mappings, retaining every field rather than
copying a second expected-field list. Agent slice groups are named explicitly
and checked against the complete dataclass group set, so adding a group requires
an explicit test update. Existing configuration readers replace duplicate
fallback readers without introducing another settings interpretation.

Faulting graph doubles forward only the graph members the handoff consumes.
Checkpoint failures, deadlines and settlement assertions remain exercised. The
signal leak guard retains its existing eight platform-available signals.

Runtime discovery remains visible for follow-up rather than being narrowed to a
snapshot of today's names. Checker rules and policy allowances are unchanged.
Messaging-feature retirement is accounted for separately from these repairs.
