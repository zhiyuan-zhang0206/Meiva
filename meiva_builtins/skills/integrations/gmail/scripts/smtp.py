# meiva:note
# The write half of the gmail skill, the effects: compose + send over SMTP, reply / forward,
# and drafts (IMAP APPEND). Side-effectful - `send` / `reply` / `forward` put real mail on
# the wire; `--dry-run` composes without sending. Split out of feed.py (2026-08-07, Task
# #1011) so the CLI entry stays under the 800-line hard ceiling.
#
# A sibling of `imap.py` and `feed.py` in this skill's `scripts/` dir, not an importable
# package -- module-only (no `__main__`/shebang), so it is always reached through `feed.py`,
# whose own `__file__` guard has already put this directory on `sys.path` by the time this
# module's `from imap import ...` runs. What a reply, a forward or a summary says is decided
# by values in gmail_compose.py.

from __future__ import annotations

import email.utils
import imaplib
import mimetypes
import smtplib
import time
from email.message import EmailMessage
from functools import cache as _cache
from pathlib import Path
from typing import Any

from gmail_auth import AuthSources, GmailError
from gmail_compose import msg_summary, plan_forward, plan_reply, recipient_list, sent_summary
from gmail_queries import message_query
from gmail_wire import ALL_MAIL, parse_drafts_folder
from imap import account, app_password, connection, fetch_full, search_ids

SMTP_HOST = "smtp.gmail.com"

# meiva:note
# STARTTLS; the App Password authenticates SMTP as well as IMAP.
SMTP_PORT = 587


# meiva:note
# Attach each file path to a composed message (turns it multipart/mixed).
#
# maintype/subtype is guessed from the filename, falling back to application/octet-stream. A
# path that is not a readable file raises rather than sending a message with a
# silently-missing attachment.
def _attach_files(msg: EmailMessage, paths: list[str]) -> None:
    for p in paths:
        path = Path(p).expanduser()
        if not path.is_file():
            raise ValueError(f"--attach path is not a readable file: {p}")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )


# meiva:note
# Build an EmailMessage (plain text, plus any file attachments) from the authenticated
# account.
def _compose(
    auth: AuthSources,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[str] | None = None,
) -> EmailMessage:
    acct = account(auth)
    msg = EmailMessage()
    msg["From"] = acct
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-Id"] = email.utils.make_msgid(domain=acct.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if attachments:
        _attach_files(msg, attachments)
    return msg


# meiva:note
# Send a composed message via Gmail SMTP (STARTTLS + App Password).
def _smtp_send(auth: AuthSources, msg: EmailMessage) -> None:
    acct = account(auth)
    password = app_password(auth)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        try:
            s.login(acct, password)
        except smtplib.SMTPAuthenticationError as e:
            raise GmailError(
                f"SMTP login failed for {acct}: {e}. The App Password authenticates SMTP too; "
                "regenerate it if this persists."
            ) from e
        s.send_message(msg)


# meiva:note
# Compose and (unless `dry_run`) send a new email from the account.
#
# SIDE-EFFECTFUL: a real email leaves the mailbox. `attach` is a list of file paths to attach.
# `dry_run=True` builds and returns the composed message (headers + body + attachment names)
# WITHOUT touching SMTP, so the caller can inspect it before committing to a real send. `sent`
# is False under dry_run.
def send(
    auth: AuthSources,
    to: str | list[str],
    subject: str,
    body: str,
    *,
    cc: str | list[str] | None = None,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    to_list = recipient_list(to)
    if not to_list:
        raise ValueError("send needs at least one recipient")
    cc_list = recipient_list(cc)
    msg = _compose(
        auth, to=to_list, subject=subject, body=body, cc=cc_list or None, attachments=attach
    )
    if not dry_run:
        _smtp_send(auth, msg)
    return sent_summary(msg, sent=not dry_run)


# meiva:note
# The message with this RFC822 Message-Id (the newest match).
def _original(auth: AuthSources, message_id: str) -> EmailMessage:
    ids = search_ids(auth, message_query(message_id))
    if not ids:
        raise GmailError(f"no message with Message-Id {message_id!r}")
    _, orig = fetch_full(auth, ids[-1])
    return orig


# meiva:note
# Reply to an existing message (looked up by RFC822 Message-Id).
#
# Threads correctly (`In-Reply-To` + `References`), `Re:`-prefixes the subject, and addresses
# the original sender (`Reply-To` if set, else `From`); `reply_all` adds the original To+Cc
# minus our own address. `attach` is a list of file paths to attach. SIDE-EFFECTFUL unless
# `dry_run` (which composes from the fetched original without sending).
def reply(
    auth: AuthSources,
    message_id: str,
    body: str,
    *,
    reply_all: bool = False,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    orig = _original(auth, message_id)
    plan = plan_reply(orig, message_id, account(auth), reply_all)
    msg = _compose(
        auth,
        to=list(plan.to),
        subject=plan.subject,
        body=body,
        cc=list(plan.cc) or None,
        in_reply_to=plan.in_reply_to,
        references=plan.references,
        attachments=attach,
    )
    if not dry_run:
        _smtp_send(auth, msg)
    return sent_summary(msg, sent=not dry_run)


# meiva:note
# Re-attach the original message's attachment parts onto a forward (real attachments and
# inline parts alike, as Gmail's own forward does).
def _carry_attachments(msg: EmailMessage, orig: EmailMessage) -> None:
    for i, part in enumerate(orig.iter_attachments()):
        payload = part.get_content()
        data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        maintype, _, subtype = part.get_content_type().partition("/")
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=part.get_filename() or f"part-{i}"
        )


# meiva:note
# Forward an existing message (looked up by RFC822 Message-Id) to `to`.
#
# `Fwd:`-prefixes the subject and quotes the original (a Gmail-style header block + the
# extracted body text) below the optional `body` note; the original's attachments are carried
# along, and `attach` adds new ones. SIDE-EFFECTFUL unless `dry_run`.
def forward(
    auth: AuthSources,
    message_id: str,
    to: str | list[str],
    *,
    body: str = "",
    cc: str | list[str] | None = None,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    orig = _original(auth, message_id)

    to_list = recipient_list(to)
    if not to_list:
        raise ValueError("forward needs at least one recipient")
    cc_list = recipient_list(cc)

    plan = plan_forward(orig, body)
    msg = _compose(
        auth,
        to=to_list,
        subject=plan.subject,
        body=plan.body,
        cc=cc_list or None,
        attachments=attach,
    )
    _carry_attachments(msg, orig)
    if not dry_run:
        _smtp_send(auth, msg)
    return sent_summary(msg, sent=not dry_run)


# meiva:note
# The mailbox's drafts folder name, discovered via its RFC 6154 `\Drafts` special-use
# attribute (the display name is localized per account; the attribute is not). Raises when
# the server advertises no such folder.
@_cache
def _drafts_folder(auth: AuthSources) -> str:
    typ, data = connection(auth).list()
    if typ != "OK":
        raise GmailError(f"LIST failed (typ={typ})")
    return parse_drafts_folder(data)


# meiva:note
# Compose a message into the Drafts folder -- nothing is sent.
#
# The draft lands via IMAP APPEND with the `\Draft` flag, so it shows up in the Drafts folder
# of every Gmail client, editable and sendable from there (the agent-drafts / human-sends
# flow). The returned `message_id` is the handle for `draft_delete`. `dry_run=True` composes
# without writing.
def draft(
    auth: AuthSources,
    to: str | list[str],
    subject: str,
    body: str,
    *,
    cc: str | list[str] | None = None,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    to_list = recipient_list(to)
    if not to_list:
        raise ValueError("draft needs at least one recipient")
    cc_list = recipient_list(cc)
    msg = _compose(
        auth, to=to_list, subject=subject, body=body, cc=cc_list or None, attachments=attach
    )
    if not dry_run:
        folder = _drafts_folder(auth)
        typ, data = connection(auth).append(
            f'"{folder}"', r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes()
        )
        if typ != "OK":
            raise GmailError(f"APPEND to {folder!r} failed (typ={typ}): {data!r}")
    return {"drafted": not dry_run, **msg_summary(msg)}


# meiva:note
# Delete a draft from the Drafts folder by its RFC822 Message-Id.
#
# Selects the Drafts folder read-write (the only write-mode select in this module), flags
# every copy `\Deleted`, expunges, then re-selects All Mail read-only to restore the
# connection's usual state. Raises when no draft carries that Message-Id.
def draft_delete(auth: AuthSources, message_id: str) -> dict[str, Any]:
    mid = message_id.strip().strip("<>")
    folder = _drafts_folder(auth)
    conn = connection(auth)
    typ, _ = conn.select(f'"{folder}"', readonly=False)
    if typ != "OK":
        raise GmailError(f"cannot select {folder!r} read-write (typ={typ})")
    try:
        ids = search_ids(auth, message_query(mid))
        if not ids:
            raise GmailError(f"no draft with Message-Id {message_id!r} in {folder!r}")
        for uid in ids:
            typ, data = conn.store(uid.decode(), "+FLAGS", r"(\Deleted)")
            if typ != "OK":
                raise GmailError(f"STORE \\Deleted on {uid!r} failed (typ={typ}): {data!r}")
        conn.expunge()
    finally:
        conn.select(ALL_MAIL, readonly=True)
    return {"deleted": len(ids), "message_id": mid, "folder": folder}
