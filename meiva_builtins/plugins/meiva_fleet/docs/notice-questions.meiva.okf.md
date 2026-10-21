---
type: doc
title: Numbered Notice Questions
description: Immutable single-select, multi-select and open notice questions with consistent numbering across Web, CLI, IM and agent replies.
tags:
- notify
- user-communication
---

# Numbered questions and answers

One notice carries one question. An agent with another question waits for the
current answer before posting it; posting while a notice is open is refused, so
close or edit the open one first.
The shared contract lives in `base.api_contracts.notice_question`.

```python
meiva.ui.notify(
    "Which surfaces should change?",
    content="Choose the scope for this iteration.",
    require_response=True,
    question={
        "type": "multi_select",
        "options": [
            {"id": "web", "label": "Web notification center"},
            {"id": "inspector", "label": "Agent inspector"},
        ],
    },
    idempotency_key="notice-scope-decision",
)
```

Options are numbered **1, 2, ... in their declared order**, not sorted by ID,
label, recommendation or selection. The same numbers appear in Web, inspector,
CLI, IM, history and agent inbounds.

There are three answer types:

- `text`: one nonblank text answer.
- `single_select`: exactly one declared option, plus an optional comment.
- `multi_select`: at least one declared option, plus an optional comment.

The Web always shows the comment field below choice options. Writing or editing
it does not change the selection, and selecting options preserves the comment.
There is no Other option or Add note button. Comments do not replace a selection.
Nothing is preselected and selecting an option never submits the answer.

Web sends `answer.option_ids` and optional `answer.comment`; text questions send
`answer.text`. The gateway validates against the stored immutable question and
stores the canonical answer and readable reply in the same transaction as the
agent inbound. CLI and IM use option numbers separated by commas on the first
line, followed by an optional multiline comment. Open questions accept plain text.
Invalid, duplicate, unknown or empty selections leave the notice unanswered.
Closing without answering delivers an explicit dismissal, never authorization.

Legacy `allow_other` and `other_text` fields remain readable so historical answers
retain their original meaning. New questions cannot enable Other and new answers
cannot submit it. Historical stored data is not rewritten.
