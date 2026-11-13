# Antigravity follows the record-read timeline

The user selected content and send order for matching Antigravity inputs, accepting that the CLI keeps no reliable injected input identifier. Adding identifiers to the model-visible text was rejected. Byte positions and conversation-scoped publication keys establish identity because the CLI's step indices can repeat or move backwards.

Cold and live reads share one parser and the existing bounded reader. Whole-row pagination is necessary: an item-count cut inside a row loses its earlier items when the next request uses an exclusive byte-offset cursor. A long background turn also needs enough earlier context to preserve reply visibility; the common context lookback can use the existing16MiB request span rather than stopping after1MiB. It keeps the existing bounded-file behavior rather than adding an Antigravity-only pager.

The structural inventory and the registered-session input evidence have separate denominators. Unregistered historical CLI files can demonstrate structural compatibility, not Meiva inbox attribution. Real replay reports retain frozen boundaries and hashes; no production bodies become fixtures.

Own synthetic experiments observed tool failure, a two-chunk record, event/file races within the sampling resolution and successful interrupt/resume with an unchanged prefix. They did not reproduce an ERROR_MESSAGE or establish the cause of historical step-index resets. Actual error-row structures establish the display mapping, while byte identity makes that unknown cause irrelevant to ordering. Finish-time reads retain their existing finite polling and allow the record to arrive after the completion signal; no new streaming or polling service was added.

This change does not authorize deployment or remove the legacy table/projector. Their removal follows a separately authorized stable Antigravity rollout.

Direct-consumer checks found an earlier gateway path merging LangGraph checkpoints into a record window. Record-backed timelines now return the current CLI window directly, including their paging count and continuation flag. This preserves one ordering and cursor authority; prior harness history stays outside that window. The stored-history path remains for Meiva agents and is not removed in this change.
