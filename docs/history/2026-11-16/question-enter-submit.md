# Question Enter submission

The user requested Enter to submit question answers and Shift+Enter to insert a
line break on 2026-11-16. The answer and Note fields use the existing batch
submission owner, retaining the last-question and pending gates. This keeps
keyboard submission subject to the same question-set and retry contract as Submit.

The question shortcut applies on every pointer type. It shares the common IME
composition guard while the conversation composer retains its pointer policy.
Suggestions still require explicit Tab or Right Arrow acceptance. This avoids
turning a placeholder into an answer when the user submits an empty draft.

The actual QuestionsForm regression failed before the change and passed after
it. Targeted coverage includes Note fields, IME confirmation, held Enter,
earlier-question gating, pending duplicate prevention and failed-intent retry.
Native keyboard interaction was not verified in this change.
