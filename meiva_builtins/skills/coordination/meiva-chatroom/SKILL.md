---
name: meiva-chatroom
description: "Runs an append-only roundtable where several agents or models debate in one Markdown file. Use for multi-model research debates and host-mediated group discussions."
---

# Chatroom — a roundtable in one Markdown file

Several agents (for example GPT, Claude and Gemini agents) take turns in one room. The room
is a single append-only Markdown file the human can read live; a helper script keeps the
sequence numbers, appends entries and wakes whoever is addressed. There is no database
table, gateway route or web UI behind it.

The helper is `"$MEIVA_PYTHON" ~/.meiva/skills/meiva-chatroom/scripts/room.py`; below it is written
`room.py <command> ...`. Commands: `init`, `add-member`, `post`, `read`, `status` (plus `ack-cursors`, which `post` calls itself).

## Roles

- **Host**: the one agent that owns the room. The room file lives in the host's own
  workspace, `<host workspace>/rooms/<slug>.md`, so the human reads it in the host's
  Workspace Files panel (Markdown, LaTeX and Mermaid render there). The human talks to the host
  in the usual chat; the host relays what they say into the room.
- **Participant**: any agent invited with this skill. It only appends through `room.py post`.

## Host workflow

1. Create the room, naming the participants as `<agent_id>:<label>`:
   `room.py init --room <workspace>/rooms/<slug>.md --title "<Title>" --host-agent <your id> --host-label "<you>" --member 8901:Claude --member 8902:GPT`
   It refuses to overwrite an existing room unless `--force`.
2. Spawn or message each participant, tell it to read this skill and give it the room spec
   `<machine>:<absolute path>` printed by `init` (a participant on another machine uses the
   same spec; see Machines below), plus its own agent id and the question.
3. Add a late joiner with `room.py add-member --room <spec> --member 8903:Gemini`; it sees only
   what is posted after it joins unless you pass `--from-start`.
4. Kick the discussion off with `room.py post --room <spec> --from <your id> --mention 8901 --mention 8902 --text "..."`.
5. Relay the human: when they want to speak into the room, post it with `--from user`
   (label defaults to `User`) and mention who should answer, or `--mention-all`. The agent
   running the command is never woken by its own post, so a relay with `--mention-all` does not
   wake the host.
6. Tell the human the room path (`rooms/<slug>.md`) so they can follow along, and summarize in
   chat when asked. Read the room yourself with `room.py read --room <spec> --tail 20`.

## Participant workflow

1. You are woken by a message that contains the unread entries and the exact reply command.
   `room.py read --room <spec> [--since <seq>] [--tail <n>]` shows more history and moves no cursor.
2. Reply only by appending: `room.py post --room <spec> --from <your id> [--mention <id> ...] --text "..."`.
   For long or multi-line text, or anything with quotes, `$`, backticks or a leading `-`, use
   `--stdin <<'CHATROOM_EOF'` (the wake message shows the exact form) or `--file <path>`. Never edit the `.md` or
   the hidden `.state/` directory by hand.
3. Keep every turn high-signal and short: new evidence, a critique of a specific claim, a
   falsifiable prediction, or a synthesis. Skip greetings and agreement for its own sake.
4. Control who speaks next with `--mention`: mention the agent whose answer you need, several to
   open a round, `--mention-all` for the whole room. Post without `--mention` to hand the floor
   back (nobody is woken). If you have nothing new to add, do not post at all.

## How it works

- `rooms/<slug>.md` is append-only: each entry is `### #004 · 2026-11-25 13:45 · Gemini (#8903) -> @8901`
  followed by the body, each entry preceded by a blank line and a `---` rule. Joins are one-line
  notes. Nothing already written is ever rewritten. A body that leaves a ``` or ~~~ code fence
  open gets the closing fence added, so it cannot swallow the entries after it.
- `rooms/.state/<slug>.json` (hidden from the file panel) holds the roster, the sequence counter, one
  cursor per member (the last entry shown to it) and the messages as data. `.state/<slug>.lock`
  serializes every change with `fcntl.flock`, so concurrent posts from many processes get distinct
  consecutive numbers and never interleave.
- A post moves the poster's own cursor, then wakes each mentioned member with
  `meiva.agents.send_message`, carrying every entry above that member's cursor (not only the newest).
  A woken member's cursor moves only after its `send_message` succeeded (the `ack-cursors` step,
  run on the room's machine). Members you do not mention keep their backlog and receive it with
  their next wake.
- Only members can post and be mentioned; `user` is the one non-member sender. A bad id, an
  empty body or a missing room exits with status 2 and changes nothing.
- A wake that fails to send is reported in the receipt (`"failed"` with the error, `"missed"` with
  the `[first, last]` entry range that target did not get; exit status 1). The entry is already in
  the room, so do not post it again. The failed target's cursor did not move: it still shows the
  old value in `status`, and its next wake carries the missed entries too. Waking it again means
  posting any message that mentions it; `read --since <seq>` shows the entries meanwhile.
- If only the cursor update fails after the wakes were sent (ssh trouble), the receipt carries
  `"unacked"` and exit status stays 0; the next wake simply repeats entries already delivered.
- The receipt is one JSON line: `{"seq": 4, "woke": [8901], "room": "ubuntu:/.../topic.md"}`.
- There is deliberately no round budget, rate limit or `@all` restriction in this version; the
  host watches the room and stops a runaway exchange by telling participants to stay silent.

## Machines

`--room` is a local path or `<machine>:<absolute path>`. When the machine is not the one you run on,
the script runs itself on that machine over `ssh` (`ssh -o BatchMode=yes -o ConnectTimeout=10 <machine> python3
"$HOME/.meiva/skills/meiva-chatroom/scripts/room.py" ...`, message body on stdin) so that agents on
different machines share one file and one lock. Requirements for a remote machine:

- The skill must be installed there, current enough to know `ack-cursors` (`meiva skill update`
  on that machine). A missing or stale install fails with exit status 2 ("No such file" or an
  unknown command) before anything is written.
- `<machine>` is used verbatim as the `ssh` host, so `~/.ssh/config` needs an alias equal to the
  Meiva machine name (`ubuntu`, `macmini`, `macbook-air`, ...), with passwordless login.
- Python 3.9+ with the standard library only.

An `ssh` that times out (120 s) exits with status 2 and says the remote command may already be
committed; run `read` or `status` before retrying so a post is not duplicated. The wake messages
are sent from the machine that ran the command; the wake messages are sent from the machine that ran the command. Use the
`<machine>:<path>` spec printed in each wake message as is.

## Limits

- One room is one file on one machine; to move a room, create a new one on the new machine.
- Hand-editing the Markdown does not update the state; the state is the source for `read` and wakes.
- Passwordless `ssh` between machines is required for cross-machine rooms (see Machines).

## Tests

`meiva_builtins/skills/coordination/meiva-chatroom/tests/` covers sequence, mention and cursor rules,
file creation and append, Markdown rendering (rule/heading separation, unclosed fences), a multi-process `flock` race, cursors that only move after a successful wake (local and over loopback `ssh`), `add-member`, `read`, `status`, wake delivery
and the cross-machine path through a loopback `ssh`.
