---
type: doc
title: Numbered Notice Questions
description: Immutable single-select, multi-select and open notice questions with consistent numbering across Web, CLI, IM and agent replies.
tags:
- notify
- user-communication
---

# Numbered questions and answers

Each Ask carries one question; several questions may remain open independently.
The shared contract lives in `base.api_contracts.notice_question`.

```python
meiva.ui.ask(
    "Which surfaces should change?",
    content="Choose the scope for this iteration.",
    question={
        "type": "multi_select",
        "options": [
            {"id": "web", "label": "Web notification center"},
            {"id": "inspector", "label": "Agent inspector"},
        ],
    },
)
```

Options are numbered **1, 2, ... in their declared order**, not sorted by ID,
label, recommendation or selection. The same numbers appear in Web, inspector,
CLI, IM, history and agent inbounds.

There are three answer types:

- `text`: one nonblank text answer, plus an optional Note.
- `single_select`: at most one declared option, plus an optional Note.
- `multi_select`: any number of declared options, plus an optional Note.

Neither choice kind requires a selection: an answer carries a selection or a
comment, and either one alone is complete (user ruling 2026-10-21). Choosing
nothing and writing a comment submits `option_ids: []` with that comment, so
"none of these, here is why" is an answer rather than a dismissal. An answer
carrying neither is still refused.

The Web always shows a Note field below the answer, for all three types.
The agent may seed it with `note_suggestion`; the user can edit or clear it. Writing or editing
it does not change the selection, and selecting options preserves the comment.
There is no Other option or Add note button. A single choice is cleared by
choosing it again, so the field that starts empty can return to empty.
Nothing is preselected and selecting an option never submits the answer.

Web sends `answer.option_ids` and optional `answer.comment`; text questions send
`answer.text` and optional `answer.comment` (the Note wire field). The gateway validates against the stored immutable question and
stores the canonical answer and readable reply in the same transaction as the
agent inbound. CLI and IM use option numbers separated by commas on the first
line, followed by an optional multiline comment. Open questions accept plain text.
A numbered reply writes `0` on that first line to choose nothing, then the
comment below it — the same comment-only answer the Web form submits, so no
channel is narrower than another. Mixing `0` with option numbers is refused, and
`0` alone carries nothing, so it is refused too.
Invalid, duplicate or unknown selections leave the notice unanswered.
Closing without answering delivers an explicit dismissal, never authorization.

Legacy `allow_other` and `other_text` fields remain readable so historical answers
retain their original meaning. New questions cannot enable Other and new answers
cannot submit it. Historical stored data is not rewritten.
