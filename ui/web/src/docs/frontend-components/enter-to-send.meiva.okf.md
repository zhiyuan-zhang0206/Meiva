---
type: doc
title: Enter sends
description: The one keyboard rule every send-type text field follows — Enter sends, Shift+Enter breaks the line, an input method keeps its keys, a touch keyboard keeps Enter as a line break — and which fields are send-type.
tags: [frontend, input, keyboard]
---

# Enter sends

`lib/input/enter-to-send.ts` owns the rule. A field's keydown handler asks
`isSendEnter(keydown, coarsePointer)` and does nothing else with the key. The
rule is a pure decision: the primary pointer arrives as an argument, read once by
the field through `lib/input/use-coarse-pointer.ts` (`useMediaQuery` over
`(pointer: coarse)`).

| Keydown | Result |
|---|---|
| Enter, no IME composition, primary pointer not coarse | sends |
| Shift+Enter | a line break at the caret — the field's own editing, untouched |
| Enter while an input method composes (`isComposing`, the legacy `Process` key, or legacy key code 229) | left to the input method: no send, and the key is not swallowed |
| Enter while `matchMedia("(pointer: coarse)")` matches | a line break; the visible Send / Submit button submits |
| any other key | untouched |

Only the send case calls `preventDefault()`. That is what keeps Shift+Enter an
ordinary edit with the caret after the break, and what lets an input method
commit a CJK candidate with Enter. A field that returned early on "Enter" without
the rule would swallow the candidate and send half a word.

## Send-type fields

Uniform, because they are all "type a message, send it":

- `components/conversation/composer.tsx` → `handleComposerKeyDown`
  (`components/conversation/composer-helpers.tsx`) — the conversation composer.
  Inside an open slash-command dropdown the same rule decides whether Enter
  selects the command or sends the draft.
- `components/notifications/open-notice-detail.tsx` — the notice reply box and,
  through `components/notifications/notice-question-form.tsx`, a question's
  answer box and its optional comment box.
- `components/agents/agent-prompt-dialog.tsx` — the fork / resurrect prompt: a
  prompt is a message to an agent.

`components/ui/auto-expand-textarea.tsx` applies the rule for every
`AutoExpandTextarea`, which is how the notice fields and the prompt dialog share
it, and takes the pointer as an optional prop so a caller (or a test) that
already knows it does not re-read it; the composer keeps its own handler
(`handleComposerKeyDown`) because its dropdown and reply-suggestion keys
interact with Enter, and receives the same value.

## Editors that keep Enter as a line break

Long-text editors are not messages: the schedules script editor and cron form,
the config JSON editor, the shell line-count field, and single-line inputs keep
Enter as a line break or as their own field commit. They are listed here so the
split is a decision, not an omission.

## Why the touch keyboard differs

A coarse pointer means an on-screen keyboard, which has no Shift key to reach
"the other" Enter. Making Enter send there would leave no way to type a second
line, so the line break wins and the button sends. The value follows the media
query's own updates, so a tablet that gains a hardware keyboard starts sending
on Enter without a reload.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md|Frontend Components]] — the catalog this node hangs from; the Composer bullet describes its states, drafts and submissions.
- [[ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md|Fleet View]] — the Inbox and inspector share `OpenNoticeDetail`, whose question form is one of these fields.
