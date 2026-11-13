# Questions above the composer

`PendingQuestions` owns the selected agent's entire awaiting-response queue,
including legacy unstructured questions. When questions are waiting, the queue
opens expanded by default and shows one question at a time, ordered by question
id. The left and right controls move between questions. The header bar shows
the count and toggles collapse. When
collapsed, the bar occupies one stable row above the composer so it
cannot obscure the last message or the scroll-to-bottom control. Answers expand
upward over the conversation without changing the timeline viewport height. The inner `ScrollArea`
viewport is capped at 45% of the visual viewport, including the on-screen
keyboard. Navigation and Submit stay below the scrolling question. `QuestionsForm`
owns the single submission, its operation key and retries.
Each question is answered or skipped (Skip is per question and can be undone, keeping
the draft), and Submit is enabled only when every question is. The submission names the
open set it was built from: when the server's set differs, nothing is settled, the latest
set replaces the agent's rows in the shared cache, drafts and skips of questions still open
stay, and the form states that the questions changed. Changing the visible question or collapsing the panel preserves every draft. Drafts and skips are kept per agent and question in the UI store
(`questionDrafts`, memory only, never written to disk or sent to the server), so going to another agent
and back keeps them; they are cleared when the question is answered and pruned when it is no longer open. A successful submission drops all of the agent's questions from
the shared question cache and refreshes resolved history. A failed feed preserves stale questions and visibly
reports the failure.

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
Tab or Right Arrow applies it as ordinary foreground text without submitting;
Enter sends only when the whole form is complete. Modified shortcuts, IME composition and
existing drafts keep their normal keyboard behavior. This same Tab/Right
acceptance applies to conversation reply suggestions.
