# JUnit publisher contracts

Task #6814 followed three unnamed Measure e2e failures in run 43135763963.
Their `verdict-junit-e2e-{1,3,4}` bundles existed, but both readers only admitted
`test-results-` bundles. The XML also omitted `file`; its pytest module classname
needed the same canonical identity as the log parser. Unfamiliar classnames keep
their published spelling rather than acquiring a guessed file path.

The first implementation of #6488 confused pytest's internal `-aN.xml` retry
number with GitHub's `run_attempt`. Real run 43137358699 contains `-a2.xml`
inside workflow attempt 1, then publishes another artifact with the same name
in workflow attempt 2. Filtering by the filename rejected valid evidence and
could admit stale evidence after a workflow rerun.

Both readers now share a publication boundary: list artifacts, verify the
current workflow attempt and its start timestamp, and select the newest
nonexpired publication per name created in that attempt's window. Missing
identity/timestamps, mismatched attempts and equally timed competing IDs stay
unknown. An older same-name publication cannot replace a current one merely
because API order changes. The recheck reader reads internal retries in order
and prefers final verdict publications; conflicting results at the same
publisher/retry remain unreadable.

Acceptance uses archived real artifacts and metadata with an isolated reader:
valid internal retry evidence remains readable; the identical older archive is
excluded after the next workflow attempt starts. No workflow rerun, production
card rewrite, UI repair or rollout is part of this change. Runtime acceptance
for #6488 remains pending deployment.

## Mixed-evidence review correction

Independent review by agent #7633 found a false-green interaction in the first
PR head: a readable raw pass returned before accumulated verdict publication
or XML errors were inspected. Mixed controls now cover missing verdict identity,
malformed verdict XML and equally timed competing IDs in either artifact order.
An error in the requested read scope makes the observation unreadable before
any passing case can close a card; the diagnostic retains the failed publication.

Partial lanes filter unrelated publications before validating their metadata
and select raw/final families separately. Thus an unrelated lane's broken
verdict does not block a valid partial result, and an exact raw name cannot
hide a valid final verdict from the same lane. Full-suite reads retain unknown
when an unreadable in-scope publication could contain the reviewed case.
