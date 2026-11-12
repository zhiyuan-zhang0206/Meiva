# Reserve orange for attention

On 2026-11-12 the user asked to remove persistent orange selection outlines and
orange Usage values, reserving orange for unread updates and pending questions,
with Task Priority explicitly retained as an exception.

The default primary and selected-surface tokens were already neutral. Focus rings
still used the brand orange in both themes. Text inputs may retain
`:focus-visible` after mouse interaction, making a legitimate focus indicator
read as a leftover orange selection. We chose neutral rings while preserving
focus visibility and existing input behavior. Removing focus feedback would
obscure keyboard navigation and would not address the shared color policy.

Normal Usage follows theme foreground; the existing warning and error conditions
use red. Hard-coded black would become unreadable in dark mode. No quota,
threshold, data computation or severity condition changes with this decision.
Optional orange state and chart accents become neutral while their labels and
category identities remain available. Unread, question and priority indicators
keep their color. Brand artwork, syntax-highlighted content and Computer Use's
independent screen marker remain outside this product-chrome change.

The current convention is maintained in the frontend palette documentation.

## Mobile attention entry

The user additionally requested a phone-only fallback on the sidebar menu:
alerts keep priority; with no alerts, add the number of unread agents and the
number of unanswered individual questions. Two unread agents and one question
therefore show three, regardless of how many unread publications each agent has.

The existing live roster and capped question feed cannot establish that exact
count. We chose one derived, read-only snapshot over the same publication seen
cursor and unanswered-question store, including non-live agents with unread
publications. This avoids a stored summary counter, an Inbox revival, per-agent
request fan-out and approximate totals from limited lists. The existing global
event repair owner refreshes the snapshot for question and publication-state
changes and reconnects. Loading and errors remain unknown states rather than
claiming zero. Desktop gains no attention aggregate entry.
