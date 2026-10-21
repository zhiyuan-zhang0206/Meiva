---
type: doc
title: Message Format
description: Message formats exchanged between agent, LLM, users, and other agents. `agent/messages/` defines inbound message construction; `_chat_inbound.py`
tags: []
---

# Message Format

## What it is

Message formats exchanged between agent, LLM, users, and other agents. `agent/messages/` defines construction of various messages; `_chat_inbound.py` assembles `kind='chat'` inbound rows into HumanMessage (including inline multimodal images).

## Message Types

### System note envelope
- Framework text the model must read as framework-originated — not as the human or a peer — is wrapped in a pair of tags: `<system_note>` on its own line, the text, then `</system_note>` on its own line. `base/lm/system_note.py` owns them: `wrap_system_note` is the one construction point, `SYSTEM_NOTE_LEAD` / `SYSTEM_NOTE_TRAIL` are the tags with the newline that always separates them from a body, and `unwrap_system_note` is the reader's inverse. The leaf sits in the LLM layer because both sides already reach it (attach packing and the message builders).
- Writers: `system_note_message` (every NoteTag note), the compaction instruction, the compact summary (`compose_summary_message`), the attachment block, and every system-sourced inbound (below).
- Inbounds keep the offset contract: `inbound_head` returns the open tag as the header, `inbound_tail` the close tag after the body, and `meiva_inbound_body_start` stays the header length. Readers render `inbound_body(text, source=…, body_start=…)` (drops the close tag) or `unwrap_system_note` when the whole payload is one note, so the framing never reaches a rendered timeline entry or catalog line.
- The injection scan (`base/agents/messages/injection_scan.py`) triggers on both tags: a message that tries to forge or close a system note is flagged like any other forged structural markup.

### Inbound Messages (`inbound_message`)
- `inbound_message(*, content, source, inbound_id, body_start, created_at=, image_urls=, message_id=)` — envelope wrapper (the header is `base/agents/messages/envelope.py:inbound_head`; `meiva_inbound_body_start` records its length so readers cut the content out by offset and never parse the header; the sender is `meiva_source`). Inbounds written before the field existed show whole, header included, in the hierarchy catalog. A system source (`system`, `system:<subtype>`) is wrapped as a system note (see above); every other source's header is a self-contained sender line.
- `source` is the original source string (`"system"` / `"agent:N"` / `"user"`), `meiva_inbound_id` records the source row id so history readers can correlate a message with its queue row. `inbound_id=None` omits that metadata key; system notes use their own message constructor.
- `content` plain text or multimodal block list
- `image_urls` keeps timeline reference URLs; model `content` may embed base64. `message_id` pins the LangChain ID.
- Two times in metadata: `meiva_created_at` keeps its meaning (an inbound's is its ARRIVAL time, the source row's `created_at`; other messages' is when they were produced). `meiva_picked_up_at` is when an injected message (inbound, note, attachment) was put into the LLM context, i.e. when the model reads it; the builders stamp it. AIMessage and tool output are produced inside the context, so their `meiva_created_at` already is the read time and they carry no `meiva_picked_up_at`. Time-ordered readers (timeline, history tree, history dump) use `message_read_time()` (`meiva_picked_up_at`, else `meiva_created_at`); delivery tracking uses `inbound_messages.created_at`. The body envelope header (`wrap_inbound`) keeps the arrival time so the agent knows when a message came in.

### Inbound timestamp ownership

`base.agents.messages.envelope.EnvelopeReadInputs` carries the producer's live
clock factory and timestamp-policy reader. `inbound_head` and `wrap_inbound`
require those inputs; construction reads neither. User and agent headers read
the display flag first and obtain a clock only when timestamps are enabled.
System and asserted-caller headers return before either reader. Arrival-time
formatting and source validation keep their existing order.

### NoteTag Enum
- Marks the source and nature of the message
- Used by agent to distinguish user messages vs agent messages vs system notifications
- canonical definition + `meiva_msg_type` discriminator (`MeivaMsgType`) + typed reading `read_meiva_kwargs()` all in `base/agents/messages/kwargs.py` (see [[messages.meiva.okf.md]])

### System Messages (`system_note_message`)
- Builds system notification messages such as heartbeats, watcher wake-up calls, and process-lifecycle transitions; the content is wrapped by `wrap_system_note`, so the model reads where the note ends
- `security_note_message(*, source, triggers, created_at=)` is the one writer of the SECURITY note for a prompt-injection scan finding (source + matched triggers, never the content); the claim node (behind a flagged inbound) and the exec node (child-drained findings) both use it

### Exec Output (`exec_output_message`)
- Wraps sandbox execution results from `execute_code`
- Contains merged stdout/stderr output
- `created_at` is a required argument: every writer (exec node, repair hook, syntax-fix plugin) stamps `meiva_created_at`, which is also the read time

### Attachments (`attach_message`)
- Appends one HumanMessage at the completed-turn boundary for files registered with `meiva.self.attach`
- The leading notice is one complete system note inside its own text block; the caption lines follow it outside the note — `[text(<system_note> notice </system_note>), text(line1), media1, text(line2), media2, ...]`. A note spanning the captions would have to interleave with media blocks, so the notice states what the block is and the lines stay exactly where the pairing needs them; `meiva_msg_type="attach"` distinguishes the message without image URL metadata, and the timeline pairs every image with its own caption line structurally

### Chat Inbound Assembly (`_chat_inbound.py`)
- Assembles `kind='chat'` inbound rows into `(HumanMessage, finding)`: plain text via envelope wrapper as string message; multimodal inbound places text as first block, then uploads referenced images as native base64 blocks inline to the model. `finding` is the injection-scan result for the row's text (None when clean); the claim node owns delivering it as a SECURITY note
- Split out from `claim/node.py`, focused on multimodal image inline path

## Key Dependencies

- [[graph.meiva.okf.md]] — message passing between LangGraph nodes
- [[system-prompt.meiva.okf.md]] — system prompt + message history = LLM input
- [[context-window.meiva.okf.md]] — message history is the main consumer of the context window

## Entry Points
- `agent/messages/__init__.py:inbound_message(*, content, source, inbound_id, body_start, created_at=, image_urls=, message_id=)` — envelope-wrapped inbound message
- `agent/messages/__init__.py:system_note_message(...)` — system notification (with `NoteTag`)
- `agent/messages/__init__.py:security_note_message(...)` — SECURITY note for one injection-scan finding
- `agent/messages/__init__.py:exec_output_message(...)` — execution output; one shape for every ending. The structured record is `meiva_exec_status` (`ExecStatus`: completed / failed / timed_out / cancelled / halted / not_run / unknown), `meiva_exec_started_at` (the end is `meiva_created_at`), `meiva_exec_ms` and `meiva_exec_body_start` (where the body begins after the header); readers use these fields and never parse the header text, which is for the model. Results written before these fields existed carry no offset; the hierarchy catalog shows them whole, header included, instead of parsing the header
- `agent/messages/__init__.py:attach_message(...)` — attached media for the next turn
- `agent/graph/claim/_chat_inbound.py` — chat inbound → (HumanMessage, scan finding) assembly (multimodal inline)
