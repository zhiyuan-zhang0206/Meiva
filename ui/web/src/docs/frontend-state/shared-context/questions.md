# Questions above the composer

`PendingQuestions` owns the selected agent's entire awaiting-response queue,
including legacy unstructured questions. When questions are waiting, the queue
opens expanded by default and shows one question at a time, ordered by question
id. Previous, next, the page count, and Submit share one bottom row.
The question grows upward to its natural height, capped at 45% of the visible
viewport. The panel reserves one row above the composer so changes in question
height leave the controls, timeline viewport, and composer in place. The collapsed row shows the pending count next to the same disclosure control.
The question area expands above the anchored footer with a short transition
when reduced motion is off. The footer controls appear and disappear together
as soon as the panel is toggled, independently of the question area's motion.
`QuestionsForm` owns the single submission, its operation key and retries.
Submit is available on the last question. Blank drafts become skipped results
when the batch is submitted. The submission names the open set it was built
from. If the set changes, the latest questions replace that agent's cached rows,
the panel reports the change, and drafts for still-open questions stay intact.
Drafts live in memory per agent and question and survive navigation, collapse,
and switching agents. Resolved and closed questions leave no drafts behind.
Successful submission removes the resolved batch from the shared cache. A
failed feed visibly reports the failure and preserves stale questions.

Pending forms appear here once. The right panel remains a document/Inspector
surface, and resolved questions arrive in the message list as ordinary system
notes rather than synthetic timeline cards. The former right-column card and
timeline form were rejected because they duplicated the answer surface and
separated it from the place the user composes a reply. A full stacked footer was rejected because expansion would shrink the message
viewport; overlaying the collapsed bar was rejected because it obscured the last
message and scroll-to-bottom control.

Open text questions have one answer box and no separate Note. Choice questions
keep numbered options and one optional Note. `note_suggestion` is a light
placeholder in the corresponding answer or Note box, not an initial draft.
Tab or Right Arrow applies it as ordinary foreground text. Enter inserts a line
break in either field. Activate Submit to send the batch. Modified shortcuts, IME
composition and existing drafts keep their normal keyboard behavior. This same Tab/Right
acceptance applies to conversation reply suggestions.
