# Selected text quotes

The user authorized a preview of selection-only chat quotes on 2026-11-17. The only entry is a nonempty selection inside one readable chat message, including manually expanded work details. The first version keeps one quote per draft, requires supplemental user text, and caps the snapshot at 4000 Unicode characters. Copy and native selection remain available.

We retain the selected text as a user-provided snapshot with typed source provenance. Existing inbound JSONB payloads carry the quote, avoiding a migration. The send identity includes the quote, and upload batches freeze it with their immutable manifest. The agent receives clearly labelled quoted context after command expansion; selected text never becomes a slash command or system instruction.

Source IDs establish provenance and navigation, while the server checks the current conversation and readable timeline independently. Rendered Markdown selections need not be substrings of raw Markdown. A source that cannot be verified is refused before sending; a saved quote remains readable even if its source later cannot be located. Native checkpoint history without verifiable provenance is an explicit first-version boundary.

Preview deployment and production deployment are separate authorizations. This contribution supplies a fixed revision for the preview operator; production rollout and merging are outside its scope.

Implementation review update: the delegator required already-loaded retained native history to remain quotable. The validator now resolves the existing historical cursor against the retained boundary index and reads that exact checkpoint segment, subject to the same configured history depth. The earlier proposed first-version exclusion of retained native history was removed before preview delivery. Source admission verifies readable provenance and existence; the selected snapshot remains user-submitted context whose textual authenticity is not asserted.

Review update: expanded work details include readable error and notification bodies. Their selected fragments use the system marker's existing stable message identity; marker headers and controls stay outside the selection body. Sources without stable marker identity are refused explicitly, retaining the draft snapshot for correction.

Async-send review update: a successful quote send clears its persisted draft reference only when that reference still matches the submitted snapshot. This preserves a replacement quote selected during an in-flight send even if the user switches conversations before completion. Direct asynchronous Composer controls cover replacement retention and unchanged-quote cleanup. External quote POST controls also exercise exact record offset arithmetic and routing for each supported external harness, including adjacent/deleted sources with zero writes.

Follow-up: the user requested full default display of sent quote snapshots; see [sent quote full display](sent-quote-full-display.md).
