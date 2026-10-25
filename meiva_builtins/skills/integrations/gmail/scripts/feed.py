"""Gmail skill -- one self-contained CLI: read over IMAP, send over SMTP.

The agent runs this as a subprocess (no SDK import, no browser), e.g.

    .venv/bin/python skills/gmail/scripts/feed.py search   --query "from:foo newer_than:7d"
    .venv/bin/python skills/gmail/scripts/feed.py read     --message-id "<abc@host>"
    .venv/bin/python skills/gmail/scripts/feed.py discover  --since 60d
    .venv/bin/python skills/gmail/scripts/feed.py enum      --list-id newsletter.example.com --since 30d
    .venv/bin/python skills/gmail/scripts/feed.py sync      --list-id newsletter.example.com --since 30d
    .venv/bin/python skills/gmail/scripts/feed.py send      --to a@b.com --subject Hi --body "..." --attach /p/f.pdf --dry-run
    .venv/bin/python skills/gmail/scripts/feed.py reply     --message-id "<abc@host>" --body "..." --dry-run
    .venv/bin/python skills/gmail/scripts/feed.py forward   --message-id "<abc@host>" --to a@b.com --dry-run
    .venv/bin/python skills/gmail/scripts/feed.py draft     --to a@b.com --subject Hi --body "..."
    .venv/bin/python skills/gmail/scripts/feed.py draft-delete --message-id "<abc@host>"

and reads the JSON printed to stdout. Eleven lenses (read over IMAP, write over
SMTP, drafts over IMAP APPEND):

  search  -- run a Gmail search, return matching messages' metadata.
  read    -- return one/few matching messages' extracted text (headers + body).
  discover-- list the distinct newsletter List-Ids seen in a time window (seed S2).
  enum    -- list one newsletter's (one List-Id's) new issues, newest-first.
  fetch   -- pull one message's .eml + attachments into the raw mirror, projected to S1.
  sync    -- enum a List-Id's new issues, fetch each, print the S1 items.
  send    -- compose and send a new email, optional file attachments (SIDE-EFFECTFUL; --dry-run to preview).
  reply   -- reply to a message by Message-Id, threaded (SIDE-EFFECTFUL; --dry-run).
  forward -- forward a message by Message-Id, original attachments carried along (SIDE-EFFECTFUL; --dry-run).
  draft   -- compose into the Drafts folder, nothing sent (editable/sendable from any Gmail client).
  draft-delete -- remove a draft from the Drafts folder by its Message-Id.

The mailbox + secret come from the macOS Keychain (one generic-password entry,
service `meiva-gmail-imap`): the account label is the login username, the secret is
a Gmail App Password (2FA-gated; the same password works for both IMAP and SMTP;
IMAP must be enabled in Gmail settings).

Detection: a message is a newsletter iff it carries a `List-Id` header -- the
durable per-newsletter identity (the From address rotates, the List-Id does not).
`category:` / `list:` / `after:` ride Gmail's `X-GM-RAW` IMAP extension, so the
full Gmail search syntax works over IMAP. Pure stdlib (`imaplib` + `smtplib` +
`email`).

`send` / `reply` / `forward` put real mail on the wire -- use `--dry-run` to
compose and inspect the message (headers + body) without sending; the caller
confirms before a real send. `draft` never sends: it APPENDs the composed
message into the mailbox's Drafts folder (found via its RFC 6154 `\\Drafts`
special-use attribute), where any Gmail client can edit and send it;
`draft-delete` removes it again by Message-Id.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

# meiva:note
# `imap`/`smtp` and the gmail_* modules are siblings in this same scripts/ dir, not an
# importable package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path) -- restore
# it for the sibling imports. Structure Rule 6 recognizes this exact __file__-derived,
# within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gmail_auth import KEYCHAIN_TIMEOUT_S, AuthSources, GmailError, GmailInputError
from gmail_queries import since_to_ts
from gmail_wire import validate_limit
from imap import discover, enum, fetch, read, search, sync, to_s1
from smtp import draft, draft_delete, forward, reply, send


# meiva:note
# Everything the CLI reads from its process, read once by the entry point: where the
# credentials come from, the raw mirror root (`~/Downloads/gmail`), the clock reading that
# `--since` windows count back from. The input stream stays with the main shell.
@dataclass(frozen=True)
class Process:
    auth: AuthSources
    mirror_root: Path
    now_ts: int


# meiva:note
# The send/reply body: `--body` if given, else the text read by main -- rejecting an
# empty/whitespace body from either source (a blank send is a mistake).
def body_arg(body: str | None, piped_body: str) -> str:
    if body is None:
        body = piped_body
    if not body.strip():
        raise GmailInputError("no body: pass a non-empty --body or pipe text on stdin")
    return body


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gmail", description="search / read / enumerate Gmail over IMAP -> JSON"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="run a Gmail search, return metadata")
    ps.add_argument("--query", required=True, help="Gmail search syntax (X-GM-RAW)")
    ps.add_argument("--limit", type=int, default=50)

    pr = sub.add_parser("read", help="return matching messages' extracted text")
    pr.add_argument("--query", help="Gmail search syntax")
    pr.add_argument("--message-id", help="RFC822 Message-Id")
    pr.add_argument("--limit", type=int, default=5)

    pd = sub.add_parser("discover", help="list distinct newsletter List-Ids (seed S2)")
    pd.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")

    for name in ("enum", "sync"):
        sp = sub.add_parser(name, help=f"{name} one newsletter (List-Id)")
        sp.add_argument("--list-id", required=True, help="List-Id value, e.g. foo.substack.com")
        sp.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")
        sp.add_argument("--limit", type=int)

    pf = sub.add_parser("fetch", help="fetch one message into the raw mirror -> S1")
    pf.add_argument("--message-id", required=True, help="RFC822 Message-Id")

    psd = sub.add_parser("send", help="compose + send a new email (side-effectful)")
    psd.add_argument("--to", required=True, help="recipient(s), comma-separated")
    psd.add_argument("--subject", required=True)
    psd.add_argument("--body", help="body text (omit to read from stdin)")
    psd.add_argument("--cc", help="cc recipient(s), comma-separated")
    psd.add_argument("--attach", action="append", help="file path to attach (repeatable)")
    psd.add_argument("--dry-run", action="store_true", help="compose + print, do not send")

    prp = sub.add_parser("reply", help="reply to a message by Message-Id (side-effectful)")
    prp.add_argument("--message-id", required=True, help="RFC822 Message-Id to reply to")
    prp.add_argument("--body", help="body text (omit to read from stdin)")
    prp.add_argument("--reply-all", action="store_true", help="also Cc the original To+Cc")
    prp.add_argument("--attach", action="append", help="file path to attach (repeatable)")
    prp.add_argument("--dry-run", action="store_true", help="compose + print, do not send")

    pfw = sub.add_parser("forward", help="forward a message by Message-Id (side-effectful)")
    pfw.add_argument("--message-id", required=True, help="RFC822 Message-Id to forward")
    pfw.add_argument("--to", required=True, help="recipient(s), comma-separated")
    pfw.add_argument("--body", default="", help="optional note above the forwarded message")
    pfw.add_argument("--cc", help="cc recipient(s), comma-separated")
    pfw.add_argument("--attach", action="append", help="extra file path to attach (repeatable)")
    pfw.add_argument("--dry-run", action="store_true", help="compose + print, do not send")

    pdr = sub.add_parser("draft", help="compose into the Drafts folder (never sends)")
    pdr.add_argument("--to", required=True, help="recipient(s), comma-separated")
    pdr.add_argument("--subject", required=True)
    pdr.add_argument("--body", help="body text (omit to read from stdin)")
    pdr.add_argument("--cc", help="cc recipient(s), comma-separated")
    pdr.add_argument("--attach", action="append", help="file path to attach (repeatable)")
    pdr.add_argument("--dry-run", action="store_true", help="compose + print, do not save")

    pdd = sub.add_parser("draft-delete", help="delete a draft by its Message-Id")
    pdd.add_argument("--message-id", required=True, help="RFC822 Message-Id of the draft")

    return p


# meiva:note
# Run the lens `args.cmd` names and return its JSON-able result.
def _run(args: argparse.Namespace, proc: Process) -> object:
    auth = proc.auth
    match args.cmd:
        case "search":
            return search(auth, args.query, limit=args.limit)
        case "read":
            return read(auth, query=args.query, message_id=args.message_id, limit=args.limit)
        case "discover":
            return discover(auth, since_ts=since_to_ts(args.since, proc.now_ts))
        case "enum":
            return enum(
                auth, args.list_id, since_ts=since_to_ts(args.since, proc.now_ts), limit=args.limit
            )
        case "sync":
            return sync(
                auth,
                args.list_id,
                root=proc.mirror_root,
                since_ts=since_to_ts(args.since, proc.now_ts),
                limit=args.limit,
            )
        case "fetch":
            return to_s1(fetch(auth, args.message_id, root=proc.mirror_root), root=proc.mirror_root)
        case "send":
            return send(
                auth,
                args.to,
                args.subject,
                args.body,
                cc=args.cc,
                attach=args.attach,
                dry_run=args.dry_run,
            )
        case "forward":
            return forward(
                auth,
                args.message_id,
                args.to,
                body=args.body,
                cc=args.cc,
                attach=args.attach,
                dry_run=args.dry_run,
            )
        case "draft":
            return draft(
                auth,
                args.to,
                args.subject,
                args.body,
                cc=args.cc,
                attach=args.attach,
                dry_run=args.dry_run,
            )
        case "draft-delete":
            return draft_delete(auth, args.message_id)
        case "reply":
            return reply(
                auth,
                args.message_id,
                args.body,
                reply_all=args.reply_all,
                attach=args.attach,
                dry_run=args.dry_run,
            )
        case _:
            raise ValueError(f"unknown command {args.cmd!r}")


# meiva:note
# The CLI: parse `argv`, run the lens against the process facts in `proc`, print the JSON.
def main(argv: Sequence[str], proc: Process, stdin: TextIO) -> None:
    args = _build_parser().parse_args(argv)
    if args.cmd in {"search", "read", "enum", "sync"}:
        validate_limit(args.limit)
    if args.cmd in {"send", "reply", "draft"}:
        args.body = body_arg(args.body, stdin.read() if args.body is None else "")
    print(json.dumps(_run(args, proc), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _home = Path.home()
    try:
        main(
            sys.argv[1:],
            Process(
                auth=AuthSources(
                    account_env=os.environ.get("GMAIL_ACCOUNT", "").strip(),
                    password_file=_home / ".meiva" / "secrets" / "gmail-app-password",
                    security_cmd="security",
                    keychain_timeout_s=KEYCHAIN_TIMEOUT_S,
                ),
                mirror_root=_home / "Downloads" / "gmail",
                now_ts=int(time.time()),
            ),
            sys.stdin,
        )
    except GmailError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
