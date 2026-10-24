# meiva:note
# What a fetched video becomes, over values: the post saved in the raw mirror from its yt-dlp
# metadata and transcript, the markdown rendering of that post and its projection onto the
# cross-platform S1 item. Nothing here runs yt-dlp, reads the clock or writes a file; the caller
# supplies the fetch time, the yt-dlp version and the mirror path.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from yt_plan import is_video_id, video_url
from yt_wire import VideoMeta

TranscriptKind = Literal["human", "auto", "whisper"]


# meiva:note
# A video's transcript as prose, and where it came from: author-provided subtitles (`human`),
# ASR auto-captions (`auto`) or the audio-transcribe skill (`whisper`).
@dataclass(frozen=True)
class Transcript:
    text: str
    kind: TranscriptKind


# meiva:note
# The uploading channel of a video.
class Channel(TypedDict):
    id: str | None
    name: str | None
    handle: str | None
    url: str | None


# meiva:note
# The engagement counters yt-dlp reports (None when it does not know them).
class Stats(TypedDict):
    views: int | float | None
    likes: int | float | None
    comments: int | float | None


# meiva:note
# The post saved in the raw mirror as `post.json`; the key order is the file's. `video_path` is
# there only when the video file was downloaded.
class Post(TypedDict):
    platform: str
    video_id: str
    url: str
    title: str | None
    description: str | None
    channel: Channel
    published_at: str | None
    duration_s: int | float | None
    tags: list[str]
    thumbnail: str | None
    stats: Stats
    transcript: str | None
    transcript_kind: TranscriptKind | None
    fetched_at: str
    fetched_via: str
    video_path: NotRequired[str]


# meiva:note
# The author an S1 item records: the channel id is the durable follow key.
class Author(TypedDict):
    id: str | None
    name: str | None


# meiva:note
# A downloaded media file attached to an S1 item.
class Media(TypedDict):
    kind: str
    path: str


# meiva:note
# A post on the cross-platform S1 item schema shared by every `web-sources` adapter (see
# `web-sources/SKILL.md`).
class S1Item(TypedDict):
    platform: str
    source_id: str
    url: str
    author: Author
    matrix_entity: str | None
    title: str | None
    text: str | None
    transcript: str | None
    published_at: str | None
    fetched_at: str | None
    fetched_via: str | None
    media: list[Media]
    stats: Mapping[str, object]
    raw_path: str


# meiva:note
# The video id, checked.
#
# Raises:
#     ValueError: `video_id` is not a YouTube video id.
def require_video_id(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError(f"not a YouTube video id: {video_id!r}")
    return video_id


def _iso_from_epoch(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.UTC).isoformat().replace("+00:00", "Z")


# meiva:note
# The post for one video from its metadata and (best-effort) transcript. A missing transcript is
# None with `transcript_kind=None`, not an error — many videos legitimately have no captions
# (music, brand-new uploads, captions disabled), so the digest falls back to title + description.
# `version` is the yt-dlp version that produced the metadata, recorded in `fetched_via`.
def build_post(
    video_id: str,
    meta: VideoMeta,
    transcript: Transcript | None,
    *,
    fetched_at: str,
    version: str,
) -> Post:
    return Post(
        platform="youtube",
        video_id=video_id,
        url=video_url(video_id),
        title=meta.title,
        description=meta.description,
        channel=Channel(
            id=meta.channel_id,
            name=meta.channel or meta.uploader,
            handle=meta.uploader_id,
            url=meta.channel_url,
        ),
        published_at=_iso_from_epoch(meta.timestamp),
        duration_s=meta.duration,
        tags=list(meta.tags),
        thumbnail=meta.thumbnail,
        stats=Stats(views=meta.view_count, likes=meta.like_count, comments=meta.comment_count),
        transcript=transcript.text if transcript is not None else None,
        transcript_kind=transcript.kind if transcript is not None else None,
        fetched_at=fetched_at,
        fetched_via=f"youtube-feed (yt-dlp {version})",
    )


# meiva:note
# The `post.md` rendering of a post.
def render_markdown(post: Post) -> str:
    ch = post["channel"]
    lines = [
        f"# {post.get('title') or post['video_id']}",
        "",
        f"- **channel**: {ch.get('name')} ({ch.get('handle')})",
        f"- **url**: {post['url']}",
        f"- **published**: {post.get('published_at')}",
        f"- **duration_s**: {post.get('duration_s')}",
        f"- **stats**: {post.get('stats')}",
        f"- **transcript**: {post.get('transcript_kind') or 'none'}",
        "",
        "## Description",
        "",
        post.get("description") or "_(none)_",
    ]
    transcript = post.get("transcript")
    if transcript:
        lines += ["", "## Transcript", "", transcript]
    return "\n".join(lines) + "\n"


# meiva:note
# Project a saved post onto the S1 item schema. `raw_path` is the mirror directory the post was
# saved under, with a trailing slash.
def to_s1(post: Post, *, matrix_entity: str | None, raw_path: str) -> S1Item:
    ch = post["channel"]
    video_path = post.get("video_path")
    media = [Media(kind="video", path=video_path)] if video_path else []
    return S1Item(
        platform="youtube",
        source_id=post["video_id"],
        url=post["url"],
        author=Author(id=ch.get("id"), name=ch.get("name")),
        matrix_entity=matrix_entity,
        title=post.get("title"),
        text=post.get("description"),
        transcript=post.get("transcript"),
        published_at=post.get("published_at"),
        fetched_at=post.get("fetched_at"),
        fetched_via=post.get("fetched_via"),
        media=media,
        stats=post.get("stats", {}),
        raw_path=raw_path,
    )
