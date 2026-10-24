# meiva:note
# Which videos a youtube run enumerates and fetches, over values: how a source spec resolves,
# which playlist entries are new relative to what the raw mirror already holds (the mirror is the
# watermark), and what a follow or a backfill takes. `yt_run.py` asks yt-dlp, reads the mirror and
# executes the plan.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from yt_wire import PlaylistEntry, SourceMeta

_RE_CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_RE_UPLOADS_ID = re.compile(r"^UU[0-9A-Za-z_-]{22}$")
_RE_PLAYLIST_ID = re.compile(r"^(?:PL|FL|LL|OL|RD)[0-9A-Za-z_-]{10,}$")
_RE_VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
_RE_HANDLE = re.compile(r"^@[0-9A-Za-z._-]+$")

SourceKind = Literal["channel", "playlist"]


# meiva:note
# A resolved enumeration target.
#
# `enum_playlist` is the playlist actually walked by `enum`: for a channel it is the uploads
# playlist; for a playlist source it is the playlist itself. `source_id` is the registry-stable id
# (the `UC...` channel id, or the `PL...` playlist id).
@dataclass(frozen=True)
class Source:
    kind: SourceKind
    source_id: str
    enum_playlist: str
    title: str | None = None


# meiva:note
# A spec that names a channel or playlist by handle or URL: yt-dlp must be asked what it is
# before it resolves to a `Source`.
@dataclass(frozen=True)
class Lookup:
    url: str


# meiva:note
# A lightweight enum result (pre-fetch): id + permalink + what little metadata flat-playlist mode
# exposes. Real `published_at`/stats arrive at fetch time. `position` is the 1-based place in the
# playlist listing.
@dataclass(frozen=True)
class FeedItem:
    video_id: str
    url: str
    title: str | None = None
    position: int | None = None


# meiva:note
# The result of walking a playlist: the items to take, newest first, and whether the walk stopped
# because it reached an already-mirrored video (a steady-state incremental run). False means the
# window ran out (or was `limit`-capped) without hitting the mirror: a first follow, or a gap
# larger than the window.
@dataclass(frozen=True)
class EnumWalk:
    items: tuple[FeedItem, ...]
    hit_watermark: bool


# meiva:note
# What a sync does: the items it enumerated (after the first-run cap), those still to fetch, and
# the ids it skips because they are already in the mirror.
@dataclass(frozen=True)
class SyncPlan:
    enumerated: tuple[FeedItem, ...]
    to_fetch: tuple[FeedItem, ...]
    skipped_ids: tuple[str, ...]


# meiva:note
# True if `text` is a YouTube video id.
def is_video_id(text: str) -> bool:
    return _RE_VIDEO_ID.match(text) is not None


# meiva:note
# The permalink of a video.
def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# meiva:note
# `UCxxxx` -> `UUxxxx`: a channel's auto-generated uploads playlist.
def _uploads_playlist(channel_id: str) -> str:
    return "UU" + channel_id[2:]


# meiva:note
# Resolve a user-facing spec: a channel id (`UC...`), an uploads playlist (`UU...`) or any
# playlist id (`PL.../FL.../...`) resolves offline; an `@handle` or a full channel/playlist URL
# needs one yt-dlp metadata call, so it comes back as the `Lookup` to make.
#
# Raises:
#     ValueError: the spec is none of these.
def plan_source(spec: str) -> Source | Lookup:
    spec = spec.strip()
    if _RE_CHANNEL_ID.match(spec):
        return Source("channel", spec, _uploads_playlist(spec))
    if _RE_UPLOADS_ID.match(spec):
        return Source("channel", "UC" + spec[2:], spec)
    if _RE_PLAYLIST_ID.match(spec):
        return Source("playlist", spec, spec)
    if _RE_HANDLE.match(spec):
        return Lookup(f"https://www.youtube.com/{spec}")
    if spec.startswith(("http://", "https://")):
        return Lookup(spec)
    raise ValueError(f"unrecognized YouTube source spec: {spec!r}")


# meiva:note
# The source a looked-up handle or URL (`spec`) resolved to. A playlist URL resolves to a
# playlist; a channel or handle URL resolves to the channel object whose `id` is the UC id.
#
# Raises:
#     ValueError: the object is neither a channel nor a playlist.
def source_from_meta(spec: str, meta: SourceMeta) -> Source:
    spec = spec.strip()
    if _RE_PLAYLIST_ID.match(meta.id):
        return Source("playlist", meta.id, meta.id, title=meta.title)
    channel_id = meta.channel_id or meta.id
    if not _RE_CHANNEL_ID.match(channel_id):
        raise ValueError(f"could not resolve {spec!r} to a channel/playlist (got id={meta.id!r})")
    return Source("channel", channel_id, _uploads_playlist(channel_id), title=meta.channel)


# meiva:note
# Walk a playlist listing newest-first and collect the videos that are not yet in the mirror.
#
# `seen` is the mirror watermark: the ids already mirrored. The walk collects until the first
# video in `seen`, then stops. An empty `seen` (a backfill / series task) stops nothing. Entries
# without a valid video id are skipped (but still count toward `position`). `limit` caps how many
# items are returned; none at all when it is 0.
def enum_items(
    entries: Sequence[PlaylistEntry], seen: frozenset[str], limit: int | None
) -> EnumWalk:
    out: list[FeedItem] = []
    hit_watermark = False
    for pos, entry in enumerate(entries, start=1):
        if limit is not None and len(out) >= limit:
            break
        vid = entry.id
        if not vid or not is_video_id(vid):
            continue
        if vid in seen:
            hit_watermark = True
            break
        out.append(FeedItem(video_id=vid, url=video_url(vid), title=entry.title, position=pos))
    return EnumWalk(items=tuple(out), hit_watermark=hit_watermark)


# meiva:note
# What a sync takes from a playlist listing, given the ids already in the mirror.
#
# A follow (a channel, steady-state incremental) walks from the top until the first mirrored
# video and takes everything above it; a first follow (or a gap larger than the window — the
# walk never reached the mirror) is capped to `initial_limit` so it does not backfill the whole
# window. `backfill` (a series task, typically a playlist) takes every video in the listing, no
# watermark stop, and skips the ones already mirrored.
def plan_sync(
    entries: Sequence[PlaylistEntry],
    mirrored: frozenset[str],
    *,
    backfill: bool,
    initial_limit: int,
) -> SyncPlan:
    walk = enum_items(entries, frozenset() if backfill else mirrored, None)
    items = walk.items
    if not backfill and not walk.hit_watermark:
        items = items[:initial_limit]
    return SyncPlan(
        enumerated=items,
        to_fetch=tuple(i for i in items if i.video_id not in mirrored),
        skipped_ids=tuple(i.video_id for i in items if i.video_id in mirrored),
    )
