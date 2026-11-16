# Restore departure bookmarks when returning to conversations

The 2026-10-17 user ruling requires returning to the visible message and its
viewport offset even when the reader left at the tail. Review of #8972 found
that the baseline timeline instead saved a follow-bottom record for readers in
the tail zone. Returning skipped the anchor and went to newly arrived output.
The chunk-before-pin gap also wrote this record because the controller was
still following. Task #7068 repairs that baseline contract separately from the
send-reserve change.

Use the existing reading-position owner to save an item bookmark on every
departure. Restore ordinary reading bookmarks as anchored, including when they
happen to land at the current tail; otherwise the next chunk would silently
resume following. Keep first visits, reader navigation to the bottom, explicit
follow-bottom records and terminal following intact. Timeline saves use the same
rule for agent switching and a remount at an existing bfcache entry; no separate
bfcache-only follow policy was found.

The current owner documents previously described two kinds of timeline departure
bookmark. That description is superseded here and updated with this repair.
The prior implementation is preserved in git history (including #8947 / #8964).
No separate historical decision file describing that two-kind rule was found.
