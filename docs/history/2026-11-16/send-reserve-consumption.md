# Consume send reserve when the reader returns to natural content

The user clarified on 2026-11-16 that sending grants temporary reading space,
which is consumed once an upward reading gesture reaches natural content, even
while the agent continues working. Commit f9e994a160 (#8964) had deferred release
until completion and allowed a working-state transition to recreate the space.
This decision supersedes those two behaviors.

Preserve the first visit to a working conversation, but let saved reserve
availability take precedence on return. A successful send can renew availability
from history without moving that reader. Neither output nor completion renews it.
The boundary is max(0, natural content height - viewport height), so the removal
is below the reader and cannot remove visible content.

Keep the existing history paging threshold. Synthetic wheel and touch controls
confirm that arriving at the top needs a fresh 64px pull, and that a committed
reader request presents its ring through delayed completion, prevents duplicate
requests and permits another pull when loading settles without new rows.
Automatic fill remains unpresented. These controls exercise DOM events and
component state; they do not establish native mobile momentum behavior.
