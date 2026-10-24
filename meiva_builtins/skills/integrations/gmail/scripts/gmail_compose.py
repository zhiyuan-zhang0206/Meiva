# meiva:note
# What a reply, a forward or a send says, as values: who a reply goes to, the subject and the
# threading headers, the quoted body of a forward, the recipient lists and the JSON summary of
# a composed message. Building the message, reading attachments and the SMTP/IMAP writes live
# in smtp.py.

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage

from gmail_auth import GmailError
from gmail_headers import addr_list, body_text


# meiva:note
# The recipients and threading of a reply. `references` is the original's References chain
# extended with its Message-Id.
@dataclass(frozen=True)
class ReplyPlan:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    in_reply_to: str | None
    references: str | None


@dataclass(frozen=True)
class ForwardPlan:
    subject: str
    body: str


# meiva:note
# A recipient argument as bare addresses: a comma-separated string is parsed, a list is used
# as given, None is empty.
def recipient_list(value: str | list[str] | None) -> list[str]:
    return addr_list(value) if isinstance(value, str) else list(value or [])


def _prefixed(subject: str, prefix: str) -> str:
    stripped = subject.strip()
    return stripped if stripped.lower().startswith(prefix.lower()) else f"{prefix} {stripped}"


# meiva:note
# The original To+Cc minus the reply's recipients and our own address, in first-seen order.
def _reply_all_cc(orig: EmailMessage, to: list[str], account: str) -> list[str]:
    cc: list[str] = []
    seen = {a.lower() for a in to} | {account.lower()}
    for addr in addr_list(orig["To"]) + addr_list(orig["Cc"]):
        if addr.lower() not in seen:
            cc.append(addr)
            seen.add(addr.lower())
    return cc


# meiva:note
# Reply to `orig` (found as `message_id`) from `account`. It goes to the original sender
# (`Reply-To` if set, else `From`), threads with In-Reply-To and References, and prefixes the
# subject `Re:`; `reply_all` adds the original To+Cc minus our own address.
def plan_reply(orig: EmailMessage, message_id: str, account: str, reply_all: bool) -> ReplyPlan:
    to = addr_list(orig["Reply-To"]) or addr_list(orig["From"])
    if not to:
        raise GmailError(f"cannot reply to {message_id!r}: original has no Reply-To/From address")
    cc = _reply_all_cc(orig, to, account) if reply_all else []
    orig_mid = (orig["Message-Id"] or "").strip()
    references = " ".join(p for p in [(orig["References"] or "").strip(), orig_mid] if p) or None
    return ReplyPlan(
        to=tuple(to),
        cc=tuple(cc),
        subject=_prefixed(orig["Subject"] or "", "Re:"),
        in_reply_to=orig_mid or None,
        references=references,
    )


# meiva:note
# Forward `orig` with the optional `note` above a Gmail-style quote of the original (a header
# block plus the extracted body text); the subject is prefixed `Fwd:`.
def plan_forward(orig: EmailMessage, note: str) -> ForwardPlan:
    quoted = "\n".join(
        [
            "---------- Forwarded message ----------",
            f"From: {(orig['From'] or '').strip()}",
            f"Date: {(orig['Date'] or '').strip()}",
            f"Subject: {(orig['Subject'] or '').strip()}",
            f"To: {(orig['To'] or '').strip()}",
            "",
            body_text(orig),
        ]
    )
    body = (note.strip() + "\n\n" + quoted) if note.strip() else quoted
    return ForwardPlan(subject=_prefixed(orig["Subject"] or "", "Fwd:"), body=body)


# meiva:note
# The JSON summary of a message. get_content() raises on a multipart (attachment) message, so
# the body is read via get_body, which returns the message itself when it is single-part.
def msg_summary(msg: EmailMessage) -> dict[str, object]:
    body_part = msg.get_body(preferencelist=("plain",))
    return {
        "message_id": (msg["Message-Id"] or "").strip().strip("<>") or None,
        "from": msg["From"],
        "to": msg["To"],
        "cc": msg["Cc"],
        "subject": msg["Subject"],
        "in_reply_to": (msg["In-Reply-To"] or "").strip().strip("<>") or None,
        "attachments": [a.get_filename() for a in msg.iter_attachments()],
        "body": (body_part.get_content() if body_part else "").strip(),
    }


def sent_summary(msg: EmailMessage, *, sent: bool) -> dict[str, object]:
    return {"sent": sent, **msg_summary(msg)}
