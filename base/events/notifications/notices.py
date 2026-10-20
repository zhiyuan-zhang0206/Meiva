from __future__ import annotations

import hashlib
import json

import psycopg

from base.events.notifications.publish import deliver_in_transaction

_SELECT_NOTICE = (
    "SELECT require_response, title, content, question FROM agent_notices WHERE id = %s"
)


def question_fingerprint(question: object) -> str:
    payload = json.dumps(question, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# meiva:note
# The key format has one owner: the delivery path that writes it and the audit
# that replays a window must agree, or the replay silently measures a different
# policy than the one the spine runs.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def notice_dedupe_key(notice_id: int, question: object) -> str:
    return f"notice:{notice_id}:{question_fingerprint(question)}"


# meiva:note
# A delivery names where its request lives: `href` is the route that shows this
# notice, which the inbox already accepts (`?notice=<global id>` selects the row).
# Without it a transport can only open a generic queue, so a user who taps the
# notification lands somewhere that does not answer "which one is this?".
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def notice_href(notice_id: int) -> str:
    return f"/fleet?notice={notice_id}"


# meiva:note
# A producer stages the delivery inside its own transaction, next to the notice
# row it just wrote: a committed notice that needs an answer always owes one
# `notification_events` row, so no crash between "notice saved" and "announce"
# can leave the notice silently undelivered. The notice row is read here rather
# than passed by the caller — the post, edit and escalation paths commit
# different payload objects, and the spine is the one owner of "which category a
# notice becomes". A notice that needs no answer is not a notification: the
# console inbox already carries it, and a ping the user cannot act on is noise.
# The dedupe key carries the question fingerprint rather than the notice id
# alone, so editing a container to add a question is a new delivery while a
# retried post of the same content stays silent. Staging cannot fail the notice:
# it runs in the notice's own transaction, so a database failure rolls both back
# and the producer retries the whole post.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def stage_notice_notification(
    conn: psycopg.Connection, *, agent_id: int, notice_id: int
) -> int | None:
    row = conn.execute(_SELECT_NOTICE, (notice_id,)).fetchone()
    if row is None:
        return None
    require_response, title, content, question = row
    if not require_response:
        return None
    staged = deliver_in_transaction(
        conn,
        category_id="core.needs_input",
        subject=title,
        body=content,
        href=notice_href(notice_id),
        dedupe_key=notice_dedupe_key(notice_id, question),
        agent_id=agent_id,
    )
    return None if staged is None else staged.id
