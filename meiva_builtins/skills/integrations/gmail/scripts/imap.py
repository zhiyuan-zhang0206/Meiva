# meiva:note
# The IMAP half of the gmail skill, the effects: reading the credentials (secrets file first,
# Keychain fallback), the login and the cached connection, search/fetch, the raw mirror on
# disk, and the feed lenses (search / read / discover / enum / fetch / save / sync). Split out
# of feed.py (2026-08-07, Task #1011) so the CLI entry stays under the 800-line hard ceiling.
# The decisions are values in gmail_auth.py, gmail_headers.py, gmail_wire.py and
# gmail_queries.py.

from __future__ import annotations

import datetime as _dt
import email.utils
import hashlib
import imaplib
import json
import mimetypes
import re
import subprocess
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as _default_policy
from functools import cache as _cache
from pathlib import Path
from typing import Any

from gmail_auth import (
    AuthSources,
    GmailError,
    account_from_keychain_dump,
    keychain_error,
    no_password_error,
    normalize_password,
)
from gmail_headers import body_text, meta
from gmail_queries import discover_query, enum_query, list_id_counts, message_query, read_query
from gmail_wire import ALL_MAIL, parse_search_response, recent, xgm
from loguru import logger

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# meiva:note
# Substack and most editorial senders prefix the plaintext body with this line; it carries
# the canonical post permalink (the S1 `url`).
_RE_VIEW_ONLINE = re.compile(r"View this post on the web at (\S+)", re.IGNORECASE)


# meiva:note
# Read the `meiva-gmail-imap` generic-password entry through the program named by `auth`;
# `extra` selects the field. No `extra` returns the attribute dump (to parse the account
# label); `("-w",)` returns the password. A missing program / denied Keychain / hung prompt
# raises `GmailError` with the cause rather than returning empty.
def keychain_read(auth: AuthSources, *extra: str) -> str:
    try:
        proc = subprocess.run(
            auth.keychain_argv(*extra),
            capture_output=True,
            text=True,
            check=False,
            timeout=auth.keychain_timeout_s,
        )
    except FileNotFoundError:
        raise keychain_error("program_missing", "", auth.keychain_timeout_s) from None
    except subprocess.TimeoutExpired:
        raise keychain_error("timed_out", "", auth.keychain_timeout_s) from None
    if proc.returncode != 0:
        raise keychain_error("refused", proc.stderr, auth.keychain_timeout_s)
    return proc.stdout


# meiva:note
# The Gmail address to log in as -- the env override, else the `acct` label of the Keychain
# entry. Cached per distinct `auth`, so the Keychain is asked once per run.
@_cache
def account(auth: AuthSources) -> str:
    if auth.account_env:
        return auth.account_env
    return account_from_keychain_dump(keychain_read(auth))


# meiva:note
# The app password. The plain-text file is read FIRST (user ruling 2026-08-26: automation
# must not depend on the macOS Keychain -- lock/prompt hangs stall headless pipelines), then
# the `meiva-gmail-imap` Keychain entry. The file path is deliberately machine-level, not
# cluster-scoped via resolve_meiva_home(): the Keychain entry it mirrors is itself per-OS-user
# (docs/conventions/dev-setup.md). A blank file or a blank Keychain secret counts as missing.
def app_password(auth: AuthSources) -> str:
    secrets_file = auth.password_file
    if secrets_file.exists():
        try:
            pwd = normalize_password(secrets_file.read_text())
            if pwd:
                return pwd
        except OSError as e:
            logger.warning("Failed to read Gmail secrets file at {}: {}", secrets_file, e)
    try:
        pwd = normalize_password(keychain_read(auth, "-w"))
        if pwd:
            return pwd
    except GmailError as e:
        logger.debug("Gmail app password not found in secrets file and Keychain unreadable: {}", e)
    raise no_password_error(secrets_file)


# meiva:note
# A logged-in connection, selected read-only on All Mail (cached per `auth` and run). Both
# credentials are resolved before the socket opens, so a missing secret fails without a
# network round trip.
@_cache
def connection(auth: AuthSources) -> imaplib.IMAP4_SSL:
    user = account(auth)
    password = app_password(auth)
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        raise GmailError(
            f"IMAP login failed for {user}: {e}. Check the app password is current and "
            "IMAP is enabled (Gmail settings -> Forwarding and POP/IMAP -> Enable IMAP)."
        ) from e
    typ, _ = conn.select(ALL_MAIL, readonly=True)
    if typ != "OK":
        raise GmailError(f"cannot select {ALL_MAIL} (typ={typ})")
    return conn


# meiva:note
# Gmail-search via X-GM-RAW; return matching sequence numbers (oldest-first).
def search_ids(auth: AuthSources, query: str) -> list[bytes]:
    typ, data = connection(auth).search(None, "X-GM-RAW", xgm(query))
    return parse_search_response(typ, data, query)


def _fetch(auth: AuthSources, uid: bytes, parts: str) -> bytes:
    typ, data = connection(auth).fetch(uid.decode(), parts)
    if typ != "OK":
        raise GmailError(f"fetch {uid!r} {parts} failed: typ={typ}")
    raw = next((p[1] for p in data if isinstance(p, tuple)), None)
    if raw is None:
        raise GmailError(f"fetch {uid!r} {parts} returned no literal")
    return raw


# meiva:note
# _class=EmailMessage: policy=email.policy.default already makes BytesParser build
# EmailMessage instances at runtime; naming it explicitly lets it type-check that way too
# (get_body/iter_attachments are EmailMessage-only methods).
def _headers(auth: AuthSources, uid: bytes) -> EmailMessage:
    return BytesParser(EmailMessage, policy=_default_policy).parsebytes(
        _fetch(auth, uid, "(BODY.PEEK[HEADER])")
    )


# meiva:note
# One message in full: its raw bytes and the parsed message.
def fetch_full(auth: AuthSources, uid: bytes) -> tuple[bytes, EmailMessage]:
    raw = _fetch(auth, uid, "(RFC822)")
    return raw, BytesParser(EmailMessage, policy=_default_policy).parsebytes(raw)


def now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


def _canonical_url(text: str) -> str | None:
    m = _RE_VIEW_ONLINE.search(text)
    return m.group(1) if m else None


# meiva:note
# Run a Gmail search (full `X-GM-RAW` syntax), newest-first, return metadata.
#
# The general "find me mail" lens -- e.g. `from:foo newer_than:7d`, `subject:invoice`,
# `category:updates`. Each item is `meta` (message_id / list_id / from / subject / date).
# 50 headers keep an interactive search page light (task #3696 exception inventory).
def search(auth: AuthSources, query: str, *, limit: int | None = 50) -> list[dict[str, Any]]:
    return [meta(_headers(auth, uid)) for uid in recent(search_ids(auth, query), limit)]


# meiva:note
# Return matching messages' extracted text (headers + body), newest-first.
#
# Exactly one selector: `query` (a Gmail search) or `message_id` (an RFC822 Message-Id, looked
# up via `rfc822msgid:`). The read lens for "open this mail" -- not the feed mirror (that is
# `fetch`). Read pulls fewer full messages (5) than search pulls headers.
def read(
    auth: AuthSources, *, query: str | None = None, message_id: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    q = read_query(query, message_id)
    out: list[dict[str, Any]] = []
    for uid in recent(search_ids(auth, q), limit):
        _, msg = fetch_full(auth, uid)
        out.append({**meta(msg), "text": body_text(msg)})
    return out


# meiva:note
# List the distinct newsletter List-Ids seen in the discovery net, to seed S2.
#
# Walks `category:updates|forums` over the window, keeps List-Id-bearing messages, and
# aggregates by List-Id: `{list_id, count, sample_from, sample_subject}` sorted by count. The
# user registers the ones they want in the memory follow-note; the skill itself stores no
# follow list.
def discover(
    auth: AuthSources, *, since_ts: int | None = None, max_scan: int = 400
) -> list[dict[str, str | int | None]]:
    ids = recent(search_ids(auth, discover_query(since_ts)), max_scan)
    return list_id_counts(meta(_headers(auth, uid)) for uid in ids)


# meiva:note
# List one newsletter's new issues newest-first (`list:<list_id> after:...`).
#
# `list_id` is the List-Id value (e.g. `newsletter.example.com`) -- the S2 follow key. `list:`
# filters server-side, so every result already belongs to this newsletter; bounded by
# `--since` (server-side) and `--limit`/`max_scan`. Each item is `meta`.
def enum(
    auth: AuthSources,
    list_id: str,
    *,
    since_ts: int | None = None,
    limit: int | None = None,
    max_scan: int = 200,
) -> list[dict[str, Any]]:
    if limit is not None and limit <= 0:
        return []
    ids = search_ids(auth, enum_query(list_id, since_ts))
    return [
        meta(_headers(auth, uid)) for uid in recent(ids, limit if limit is not None else max_scan)
    ]


# meiva:note
# A filesystem-safe mirror dir name derived from the RFC822 Message-Id.
def _safe_id(message_id: str) -> str:
    core = message_id.strip().strip("<>")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", core)
    # meiva:note
    # Empty, or a pure dot-segment (`.` / `..`): both would resolve to the mirror root or its
    # parent (a Message-Id is attacker-set), so hash it instead.
    if not safe or set(safe) <= {"."}:
        return hashlib.sha256(message_id.encode()).hexdigest()[:16]
    if len(safe) > 120:
        return safe[:80] + "-" + hashlib.sha256(core.encode()).hexdigest()[:12]
    return safe


# meiva:note
# The mirror dir for a message, asserting it stays inside `base` (the Message-Id is
# sender-set, so containment is checked, not trusted).
def _outdir(message_id: str, base: Path) -> Path:
    outdir = base / _safe_id(message_id)
    if not outdir.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"refusing to write outside the mirror root: {outdir}")
    return outdir


# meiva:note
# A single-component, traversal-safe name for an attachment. The sender sets the filename, so
# drop directory parts + unsafe chars and refuse leading dots (no `.`/`..`/hidden); a nameless
# part is named from `fallback` + a guessed ext.
def _safe_filename(raw: str | None, *, fallback: str, content_type: str) -> str:
    base = (raw or "").replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    if not base:
        base = fallback + (mimetypes.guess_extension(content_type) or ".bin")
    return base[:120]


# meiva:note
# Write every non-body part to `<outdir>/attachments/`, return the manifest.
#
# Real attachments and inline parts (e.g. a signature logo) alike are saved -- `disposition`
# ('attachment'/'inline') tells them apart -- each to a deduplicated, traversal-safe filename.
# A row is `{filename, content_type, disposition, size, path}`. No parts means an empty list
# and no dir created.
def _attachments(msg: EmailMessage, outdir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    used: set[str] = set()
    adir = outdir / "attachments"
    for i, part in enumerate(msg.iter_attachments()):
        ctype = part.get_content_type()
        name = base = _safe_filename(
            part.get_filename(), fallback=f"attachment-{i}", content_type=ctype
        )
        n = 1
        # meiva:note
        # A collision within one message suffixes the stem.
        while name in used:
            name = f"{Path(base).stem}_{n}{Path(base).suffix}"
            n += 1
        used.add(name)
        payload = part.get_content()
        data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        adir.mkdir(parents=True, exist_ok=True)
        (adir / name).write_bytes(data)
        rows.append(
            {
                "filename": name,
                "content_type": ctype,
                "disposition": part.get_content_disposition(),
                "size": len(data),
                "path": str(adir / name),
            }
        )
    return rows


# meiva:note
# Fetch one message into the raw mirror `<root>/<safe-id>/` (the CLI's root is
# `~/Downloads/gmail`).
#
# Looks the message up by its RFC822 Message-Id (`rfc822msgid:`), writes `raw.eml`
# (never-deleted raw) + `post.json` + `post.md` + any attachments under `attachments/`, and
# returns the curated post dict (with an `attachments` manifest). Body text is the plain part
# (or stripped html).
def fetch(auth: AuthSources, message_id: str, *, root: Path) -> dict[str, Any]:
    # meiva:note
    # The CLI passes <id>, sync passes the bare id; canonicalize.
    message_id = message_id.strip().strip("<>")
    ids = search_ids(auth, message_query(message_id))
    if not ids:
        raise GmailError(f"no message with Message-Id {message_id!r}")
    raw, msg = fetch_full(auth, ids[-1])
    msg_meta = meta(msg)
    if not msg_meta["message_id"]:
        raise GmailError(f"message {message_id!r} has no Message-Id header")
    text = body_text(msg)
    from_name = email.utils.parseaddr(msg_meta["from"] or "")[0] or msg_meta["from"]
    outdir = _outdir(msg_meta["message_id"], root)
    outdir.mkdir(parents=True, exist_ok=True)
    post: dict[str, Any] = {
        "platform": "gmail",
        "message_id": msg_meta["message_id"],
        "list_id": msg_meta["list_id"],
        "url": _canonical_url(text),
        "title": msg_meta["subject"],
        "text": text,
        "author": {"id": msg_meta["list_id"], "name": from_name},
        "published_at": msg_meta["date"],
        "list_unsubscribe": (msg["List-Unsubscribe"] or "").strip() or None,
        "attachments": _attachments(msg, outdir),
        "fetched_at": now_iso(),
        "fetched_via": "gmail",
    }
    save(post, raw, root=root)
    return post


# meiva:note
# Persist a fetched message to `<root>/<safe-id>/` (raw.eml + post.json + post.md; any
# attachments are written separately by `fetch`).
def save(post: dict[str, Any], raw_eml: bytes, *, root: Path) -> Path:
    if not post.get("message_id"):
        raise ValueError("refusing to save a message with no Message-Id")
    outdir = _outdir(post["message_id"], root)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "raw.eml").write_bytes(raw_eml)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(_render_markdown(post))
    return outdir


def _render_markdown(post: dict[str, Any]) -> str:
    a = post["author"]
    lines = [
        f"# {post.get('title') or post['message_id']}",
        "",
        f"- **from**: {a.get('name')} (list-id {a.get('id')})",
        f"- **url**: {post.get('url') or '_(no web permalink)_'}",
        f"- **published**: {post.get('published_at')}",
        "",
        post.get("text") or "_(empty body)_",
    ]
    return "\n".join(lines) + "\n"


# meiva:note
# Project a fetched message onto the cross-platform S1 item schema shared with the
# `web-sources` adapters (see `web-sources/SKILL.md`). author.id = List-Id (the durable
# per-newsletter key); newsletters are singletons, so matrix_entity stays null. `root` is the
# mirror root the message was saved under, which is where `raw_path` points.
def to_s1(post: dict[str, Any], *, root: Path, matrix_entity: str | None = None) -> dict[str, Any]:
    a = post["author"]
    # meiva:note
    # S1 `media` is image|video only; other attachments (pdf, ...) still land on disk + in
    # post.json `attachments`, they just are not "media" in S1 terms.
    media = [
        {"kind": att["content_type"].split("/", 1)[0], "path": att["path"]}
        for att in post.get("attachments", [])
        if att["content_type"].split("/", 1)[0] in ("image", "video")
    ]
    return {
        "platform": "gmail",
        "source_id": post["message_id"],
        "url": post.get("url"),
        "author": {"id": a.get("id"), "name": a.get("name")},
        "matrix_entity": matrix_entity,
        "title": post.get("title"),
        "text": post.get("text"),
        "transcript": None,
        "published_at": post.get("published_at"),
        "fetched_at": post.get("fetched_at"),
        "fetched_via": post.get("fetched_via"),
        "media": media,
        "stats": {},
        "raw_path": str(root / _safe_id(post["message_id"])) + "/",
    }


# meiva:note
# Return the saved post if its raw mirror already exists (re-runs over the same `--since`
# window reuse it instead of re-fetching) -- Message-Id is the stable, account-independent
# dedup key.
def _load_if_mirrored(message_id: str, *, root: Path) -> dict[str, Any] | None:
    post_json = root / _safe_id(message_id) / "post.json"
    if not post_json.exists():
        return None
    return json.loads(post_json.read_text())


# meiva:note
# Enumerate a newsletter's new issues, fetch each, return the S1 items.
#
# Issues already in the raw mirror are reused (Message-Id dedup), so re-running the same
# window each digest is naturally idempotent.
def sync(
    auth: AuthSources,
    list_id: str,
    *,
    root: Path,
    since_ts: int | None = None,
    limit: int | None = None,
    matrix_entity: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in enum(auth, list_id, since_ts=since_ts, limit=limit):
        mid = it["message_id"]
        if not mid:
            continue
        post = _load_if_mirrored(mid, root=root) or fetch(auth, mid, root=root)
        out.append(to_s1(post, root=root, matrix_entity=matrix_entity))
    return out
