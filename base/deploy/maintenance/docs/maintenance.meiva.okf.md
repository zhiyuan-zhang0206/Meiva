---
type: doc
title: Native pause and maintenance
description: A durable home-local admission hold around existing restart, claim and checkpoint boundaries, shared by stop, restart and update.
status: current
---

# Native pause and maintenance

`admission.py`, `state.py` and `cohort.py` extend the
[pause-owner journal](pause_owner.meiva.okf.md). There is no new database
pause table or agent graph hook. The exact `(holder, acquired_at)` operation is
stored in `$MEIVA_HOME/run/deploy-pause-owner.json`; it has no TTL. Invalid or
unreadable state refuses admission. External-agent identity leases are a
separate protocol and are not acquired or released by native maintenance.

`ops.agent_pause` publishes the hold before locking native `agents_meta` rows.
Hosted admission checks it at the row-lock boundary. Existing
iterations run until an ordinary `restart` reaches claim. Lifecycle priority
preserves pending ordinary messages. A restart arriving after claim can allow
one more iteration; this is not an instruction-level freeze.

The journal records one restart ID per captured live incarnation. Preparation
first records the cohort, commits commands, then records their IDs. A retry
reuses the same commands. Normal claim binds the target generation and owner.
Only an agent with work in flight must have the live original owner. An idle
row whose owner is not the host being restarted (none, or a departed boot's) is
parked when lifecycle work is settled, whether or not a host is running; its next
wake is an ordinary re-admission under the new boot. Running turns and ambiguous
work refuse drain.

A host that crashes during the drain restarts with a new boot owner: held
admission lets a row with an unfinished restart/terminate command through
whichever boot owns it, so the new host runs it and records the drain. Nothing needs cancelling.

Hosted drain receipts require the shielded continuation and final checkpoint
flush to finish. The matching DB restart must
be done with its release marker, over an idle row whose runtime identity is
released: the apply executes and closes the restart in one transaction, so that
single fact is the proof, whichever host looks next. Held lifecycle controls
flush any prior buffered checkpoint before claiming their restart, without
invoking the graph or consuming ordinary messages; an idle row alone is insufficient.

A turn that fails while its continuation is still owed to a draining hold
(`agent_host/lifecycle/maintenance.py`) ends that agent's restart command once:
an open command is settled `done` with a `failed` lifecycle result
(`continuation_failed`), ERROR is logged and
`agent_continuation_lost` (read by an alert rule) is raised once, and the pending
wake scan no longer drives that agent. Nothing is latched in the journal and
nothing is fenced. A restart that had already executed is done and is not
rewritten. The agent stays in the hold: it never drains, so
`meiva stop` ends at its drain deadline with the stall report, and a person takes
it from there; `meiva start` releases the hold. The failed turn's own work may be
lost, and an open restart that was ended is not carried out.

Phases are `preparing → draining → drained → stopping → stopped`, owned by
`state.MaintenancePhase`; `meiva stop` / `meiva restart` walk all five. Journal
reads validate the enum. There is no start phase: `meiva start` authorizes bring-up
against the retained hold and releases it after actual readiness. Nested calls
pass the authorized operation snapshot and verify its holder and acquisition time.

`meiva start` releases the hold once the unit serves; its resume wakes every
restart pointer. It does not replay the original graph or code execution. Pending
restart controls remain subject to resource and lifecycle gates; unknown compact
or cancel attempts stay held. New ordinary execution requires fresh input.

Repeating a completed stop reads the journal before configuration
bootstrap. First-time normal drain still needs data-plane configuration.


SDK dependencies remain available through prepare/drain. Service stop closes
new ordinary ops admission and waits for admitted handlers and executor work before
signalling services. `meiva restart` always retains the data plane; whether it keeps
the shell sessions, the shared browser and the extras is the operator's explicit
choice, as it is for `meiva stop`, which also stops home-owned infrastructure unless
`--keep-infra` is given. `service_stop` verifies process identities
and exits; `data_plane/maintenance_stop` saves Redis before its verified shutdown.
`_stop_extras` covers home-owned Gate/helper/native LGTM outside the session
roster, retaining desired configuration and data. None of these local checks
proves that every remote or unregistered writer has stopped.

During a stopped hold, gateway `GET /api/health` remains a control-plane
identity and real database probe. It remains public and reports database
failure as degraded; it does not certify business admission. Ops `status_probe`
also stays reachable and remains counted through completion. Its existing
authentication requirements are unchanged.
Resume still checks the exact generation and actual serving
state. Ordinary start proves readiness before releasing the hold; an exit-code
waiver never certifies serving. Business APIs and native work stay closed until
that successful resume.

Explicit force kills native processes without stamping a normal termination or
inventing a restart/flush receipt. It preserves original metadata for inspection;
crash reaping reports termination without resurrecting or replaying old work.

`meiva status` reads the hold journal (`meiva status --json` prints only the hold) and
`meiva start` is its only exit, over a hold in any phase. It cannot prove replay
safety for a failed arbitrary external effect. A stop that failed past the drain
is retried with `meiva stop`, or finished with `meiva start`.

See [operator procedure](../../../../docs/conventions/operations/graceful-maintenance.md) for
resource scopes, recovery and the first-deployment limitation.

## Readiness test boundary

`tests/components/services/test_maintenance_readiness.py` verifies that held
bootstrap still requires authentication and omits database login credentials,
while business admission stays closed. Its first-join case runs the real gateway
middleware through TestClient by replacing `dial_get`; it skips `install_bundle`'s
PostgreSQL login probe because the fixture's issued login records are synthetic.
This proves the held bootstrap path, not native network access or a successful
login with newly installed roles.
