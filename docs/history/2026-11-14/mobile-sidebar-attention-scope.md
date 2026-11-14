# Mobile sidebar attention scope and theme color

On 2026-11-14 the user reported a deep-orange `99+` phone sidebar badge and
expected `1` using the theme orange. The previous decision selected a deeper
orange for small white digits; the user correction chooses the shared sunset
orange with white digits. Its measured white contrast is approximately 2.56:1
in both themes, below the small-text AA threshold. We preserve the requested
color instead of silently substituting another shade.

The prior decision is preserved in
[Reserve orange for attention](../2026-11-12/reserve-orange-for-attention.md).

## Unread scope correction

A 21:10:34 CST production read-only snapshot found 120 unread agents: two live
and 118 terminated. At 21:11:19 the formal attention endpoint returned 120
unread agents and zero open questions; the alerts endpoint returned zero
unresolved alerts. This explains a current `99+` without asserting the precise
count or branch at the earlier screenshot time.

We now match unread-agent eligibility to the live sidebar roster. The prior
choice to include archived unread publications made the closed-sidebar badge
represent work outside that sidebar's current scope. We preserve archived
publications and their seen cursors so revival restores unread eligibility.
Each unanswered question still contributes independently of owner lifecycle;
termination does not answer a question. The total remains a derived snapshot
rather than a persisted counter, and no user data is changed by this repair.

We rejected a fixed `1`, advancing seen cursors, or dismissing questions to match
the screenshot. The live count can change as conversations are read. Existing
lifecycle, publication and question hints plus reconnect repair the snapshot;
alerts retain priority and desktop controls retain their existing behavior.
