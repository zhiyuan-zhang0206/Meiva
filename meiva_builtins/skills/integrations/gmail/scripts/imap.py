# meiva:note
# The IMAP half of the gmail skill, the effects: reading the credentials (secrets file first,
# Keychain fallback), the login and the cached connection, search/fetch, the raw mirror on
# disk, and the feed lenses (search / read / discover / enum / fetch / save / sync). Split out
# of feed.py (2026-08-07, Task #1011) so the CLI entry stays under the 800-line hard ceiling.
# The decisions are values in gmail_auth.py, gmail_headers.py, gmail_wire.py and
# gmail_queries.py.

from __future__ import annotations

import email.utils
import hashlib
import imaplib
import json
import mimetypes
import re
import subprocess
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as _default_policy
from functools import cache as _cache
from pathlib import Path
from typing import TypedDict

from gmail_auth import (
    AuthSources,
    GmailError,
    account_from_keychain_dump,
    keychain_error,
    no_password_error,
    normalize_password,
)
from gmail_headers import body_text, header, meta
from gmail_queries import discover_query, enum_query, list_id_counts, message_query, read_query
from gmail_wire import ALL_MAIL, parse_search_response, recent, xgm
from loguru import logger

from base.host.boundary.fields import (
    int_field,
    optional_list_field,
    optional_object_field,
    optional_str_field,
    str_field,
)
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


# meiva:note
# The author block a mirrored post and an S1 item both carry: the newsletter's durable id (its
# List-Id, which survives a From address change) and the display name the message came from.
class Author(TypedDict):
    id: str | None
    name: str | None


# meiva:note
# One saved attachment, as the mirror's manifest records it: the traversal-safe name it landed
# under, the MIME type that named it, whether the sender marked it inline, its size, and its path.
class Attachment(TypedDict):
    filename: str
    content_type: str
    disposition: str | None
    size: int
    path: str


# meiva:note
# The curated post `fetch` returns and `save` writes as `post.json`: one message as a feed item,
# with the body text, its canonical permalink and the attachment manifest.
class Post(TypedDict):
    platform: str
    message_id: str
    list_id: str | None
    url: str | None
    title: str | None
    text: str
    author: Author
    published_at: str | None
    list_unsubscribe: str | None
    attachments: list[Attachment]
    fetched_at: str
    fetched_via: str


# meiva:note
# One entry of an S1 item's `media`: the kind of asset (its MIME major type) and where it landed.
class S1Media(TypedDict):
    kind: str
    path: str


# meiva:note
# The cross-platform S1 item `to_s1` projects a post onto: the schema the `web-sources` adapters
# share, with the newsletter's List-Id as the author and no matrix entity (issues are singletons).
class S1Item(TypedDict):
    platform: str
    source_id: str
    url: str | None
    author: Author
    matrix_entity: str | None
    title: str | None
    text: str | None
    transcript: str | None
    published_at: str | None
    fetched_at: str | None
    fetched_via: str | None
    media: list[S1Media]
    stats: dict[str, object]
    raw_path: str


# meiva:note
# An instant as the ISO-8601 UTC stamp the raw mirror records in `fetched_at`
# (`2026-06-22T00:00:00Z`); seconds only, so every mirror of one second renders alike.
def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _canonical_url(text: str) -> str | None:
    m = _RE_VIEW_ONLINE.search(text)
    return m.group(1) if m else None


# meiva:note
# Run a Gmail search (full `X-GM-RAW` syntax), newest-first, return metadata.
#
# The general "find me mail" lens -- e.g. `from:foo newer_than:7d`, `subject:invoice`,
# `category:updates`. Each item is `meta` (message_id / list_id / from / subject / date).
# 50 headers keep an interactive search page light (task #3696 exception inventory).
def search(auth: AuthSources, query: str, *, limit: int | None = 50) -> list[dict[str, str | None]]:
    return [meta(_headers(auth, uid)) for uid in recent(search_ids(auth, query), limit)]


# meiva:note
# Return matching messages' extracted text (headers + body), newest-first.
#
# Exactly one selector: `query` (a Gmail search) or `message_id` (an RFC822 Message-Id, looked
# up via `rfc822msgid:`). The read lens for "open this mail" -- not the feed mirror (that is
# `fetch`). Read pulls fewer full messages (5) than search pulls headers.
def read(
    auth: AuthSources, *, query: str | None = None, message_id: str | None = None, limit: int = 5
) -> list[dict[str, str | None]]:
    q = read_query(query, message_id)
    out: list[dict[str, str | None]] = []
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
) -> list[dict[str, str | None]]:
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
def _attachments(msg: EmailMessage, outdir: Path) -> list[Attachment]:
    rows: list[Attachment] = []
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
        # meiva:note
        # `get_content` answers `Any` (a part's payload may be bytes, a nested message or text), so
        # it is read as an opaque value and encoded when it is not already bytes.
        payload = object_of(part.get_content)
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
def fetch(auth: AuthSources, message_id: str, *, root: Path, now_ts: float) -> Post:
    # meiva:note
    # The CLI passes <id>, sync passes the bare id; canonicalize.
    message_id = message_id.strip().strip("<>")
    ids = search_ids(auth, message_query(message_id))
    if not ids:
        raise GmailError(f"no message with Message-Id {message_id!r}")
    raw, msg = fetch_full(auth, ids[-1])
    msg_meta = meta(msg)
    # meiva:note
    # A message without a Message-Id has no stable key to mirror it under, so it is refused; binding
    # it here is also what makes the rest of the function hold a `str`.
    stored_id = msg_meta["message_id"]
    if not stored_id:
        raise GmailError(f"message {message_id!r} has no Message-Id header")
    text = body_text(msg)
    from_name = email.utils.parseaddr(msg_meta["from"] or "")[0] or msg_meta["from"]
    outdir = _outdir(stored_id, root)
    outdir.mkdir(parents=True, exist_ok=True)
    post: Post = {
        "platform": "gmail",
        "message_id": stored_id,
        "list_id": msg_meta["list_id"],
        "url": _canonical_url(text),
        "title": msg_meta["subject"],
        "text": text,
        "author": {"id": msg_meta["list_id"], "name": from_name},
        "published_at": msg_meta["date"],
        "list_unsubscribe": (header(msg, "List-Unsubscribe") or "").strip() or None,
        "attachments": _attachments(msg, outdir),
        "fetched_at": _iso_utc(now_ts),
        "fetched_via": "gmail",
    }
    save(post, raw, root=root)
    return post


# meiva:note
# Persist a fetched message to `<root>/<safe-id>/` (raw.eml + post.json + post.md; any
# attachments are written separately by `fetch`).
def save(post: Post, raw_eml: bytes, *, root: Path) -> Path:
    message_id = post["message_id"]
    if not message_id:
        raise ValueError("refusing to save a message with no Message-Id")
    outdir = _outdir(message_id, root)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "raw.eml").write_bytes(raw_eml)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(_render_markdown(post))
    return outdir


def _render_markdown(post: Post) -> str:
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
def to_s1(post: Post, *, root: Path, matrix_entity: str | None = None) -> S1Item:
    a = post["author"]
    # meiva:note
    # S1 `media` is image|video only; other attachments (pdf, ...) still land on disk + in
    # post.json `attachments`, they just are not "media" in S1 terms. `attachments` is read as a
    # required key because every `Post` that reaches here was built by `fetch` or by
    # `_post_from_mirror`, which fills `[]` for a `post.json` written before the key existed -- a
    # hand-built dict without it is outside that contract.
    media: list[S1Media] = [
        {"kind": att["content_type"].split("/", 1)[0], "path": att["path"]}
        for att in post["attachments"]
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
# The post a previous run wrote into the mirror, read back as the value it was written as. The file
# is this skill's own, but reading it decodes untyped JSON, so each field is taken through the
# boundary readers and a missing or mistyped one raises `GmailError` naming the file rather than
# flowing on as a wrong type.
def _post_from_mirror(raw: object, path: Path) -> Post:
    if not is_object_dict(raw):
        raise GmailError(f"mirror {path} does not hold a JSON object")
    # meiva:note
    # The boundary readers raise a bare `TypeError` naming the key (`text is missing, not str`),
    # which says nothing about WHICH mirror file is broken; every read below is wrapped so the
    # failure an operator sees names the file.
    try:
        author = optional_object_field(raw, "author") or {}
        attachments: list[Attachment] = []
        for item in optional_list_field(raw, "attachments") or []:
            if not is_object_dict(item):
                raise GmailError(f"mirror {path} holds an attachment that is not a JSON object")
            attachments.append(
                Attachment(
                    filename=str_field(item, "filename"),
                    content_type=str_field(item, "content_type"),
                    disposition=optional_str_field(item, "disposition"),
                    size=int_field(item, "size"),
                    path=str_field(item, "path"),
                )
            )
        return Post(
            platform=str_field(raw, "platform"),
            message_id=str_field(raw, "message_id"),
            list_id=optional_str_field(raw, "list_id"),
            url=optional_str_field(raw, "url"),
            title=optional_str_field(raw, "title"),
            text=str_field(raw, "text"),
            author={
                "id": optional_str_field(author, "id"),
                "name": optional_str_field(author, "name"),
            },
            published_at=optional_str_field(raw, "published_at"),
            list_unsubscribe=optional_str_field(raw, "list_unsubscribe"),
            attachments=attachments,
            fetched_at=str_field(raw, "fetched_at"),
            fetched_via=str_field(raw, "fetched_via"),
        )
    except TypeError as error:
        raise GmailError(f"mirror {path}: {error}") from error


# meiva:note
# Return the saved post if its raw mirror already exists (re-runs over the same `--since`
# window reuse it instead of re-fetching) -- Message-Id is the stable, account-independent
# dedup key.
def _load_if_mirrored(message_id: str, *, root: Path) -> Post | None:
    post_json = root / _safe_id(message_id) / "post.json"
    if not post_json.exists():
        return None
    # meiva:note
    # `json.loads` answers `Any`; `object_of` hands the decoded document back as an `object` for
    # `_post_from_mirror` to read field by field.
    return _post_from_mirror(object_of(json.loads, post_json.read_text()), post_json)


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
    now_ts: float,
    since_ts: int | None = None,
    limit: int | None = None,
    matrix_entity: str | None = None,
) -> list[S1Item]:
    out: list[S1Item] = []
    for it in enum(auth, list_id, since_ts=since_ts, limit=limit):
        mid = it["message_id"]
        if not mid:
            continue
        post = _load_if_mirrored(mid, root=root) or fetch(auth, mid, root=root, now_ts=now_ts)
        out.append(to_s1(post, root=root, matrix_entity=matrix_entity))
    return out
