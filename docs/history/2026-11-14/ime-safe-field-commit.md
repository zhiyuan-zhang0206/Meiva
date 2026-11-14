# Preserve IME confirmation in single-line commit fields

Guide, Schedule and package draft inputs and agent rename had custom Enter
handlers that could submit when a user confirmed an IME candidate. Inspection
also found the same path in inline configuration text editing.

Reuse the existing `isImeComposition` decision (native composition, Process,
and legacy 229) before these handlers act. This keeps Safari's confirming
Enter safe without adding a second composition state tracker. Single-line
commit fields retain their own Shift+Enter and touch behavior; adopting chat's
`isSendEnter` policy would change those controls unnecessarily. Rename and config
also leave composition Escape to the input method.

The shell numeric line-count field remains outside the natural-language scope.
A scan of current documentation and future plans found no roadmap item to close;
the component's Enter policy records the distinction between send and commit.
