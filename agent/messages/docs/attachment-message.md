# Attachment Message

## What it is

- One HumanMessage for the files registered with `meiva.self.attach`, appended right after the `execute_code` result of the call that registered them (the model sees it at its next step of the same turn); the claim-node drain appends it at the turn boundary only on the paths that skip the exec update (compact halt)
- The leading notice is one complete system note inside its own text block; the caption lines follow it outside the note — `[text(<system_note> notice </system_note>), text(line1), media1, text(line2), media2, ...]`. A note spanning the captions would have to interleave with media blocks, so the notice states what the block is and the lines stay exactly where the pairing needs them; `meiva_msg_type="attach"` distinguishes the message without image URL metadata, and the timeline pairs every image with its own caption line structurally

`meiva.self.attach` is the agent's way to look at any local media file itself (user uploads, screenshots, downloaded or generated images, not only files generated this turn); the other way is `meiva.understand`, whose separate model answers in text and keeps the context small ([meiva.understand — Multimodal Understanding Primitive](../../../meiva/understand/README.md), [meiva.self — Agent Self](../../../meiva/self/README.md)). Timing and drain: [Tool Execution — Fault-Isolated Code Execution](../../graph/exec/docs/exec.md).

Part of [Message Format](message-format.md).
