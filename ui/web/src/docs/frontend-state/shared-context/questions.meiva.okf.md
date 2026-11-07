---
type: doc
title: Conversation question queue
description: Multiple pending questions above the composer, stable timeline geometry and explicit suggestion acceptance.
tags:
- frontend
---

# Questions above the composer

`PendingQuestions` owns the selected agent's entire awaiting-response queue,
including legacy unstructured questions. The default collapsed bar shows the
count; expanding shows independently answerable questions ordered by priority
and age. The collapsed bar occupies one stable row above the composer so it
cannot obscure the last message or the scroll-to-bottom control. Answers expand
upward over the conversation without changing the timeline viewport height. The inner `ScrollArea`
viewport is capped at 45% of the visual viewport, including the on-screen
keyboard. The existing `OpenNoticeDetail` owns submission, operation keys,
conflicts and retries. Collapse hides mounted forms, preserving every draft.
A successful reply drops only that question from the shared notice cache and
refreshes resolved history. A failed feed preserves stale questions and visibly
reports the failure.

Pending forms appear here once. The right panel remains a document/Inspector
surface, and the timeline keeps resolved question/answer history. Suggestion
requests still receive pending-question context, even though those questions
are outside the visible timeline. The former right-column card and timeline
form were rejected because they duplicated the answer surface and separated it
from the place the user composes a reply. A full stacked footer was rejected because expansion would shrink the message
viewport; overlaying the collapsed bar was rejected because it obscured the last
message and scroll-to-bottom control.

Open text questions have one answer box and no separate Note. Choice questions
keep numbered options and one optional Note. `note_suggestion` is a light
placeholder in the corresponding answer or Note box, not an initial draft.
Tab or Right Arrow applies it as ordinary foreground text without submitting;
Enter sends only a nonempty draft. Modified shortcuts, IME composition and
existing drafts keep their normal keyboard behavior. This same Tab/Right
acceptance applies to conversation reply suggestions.
