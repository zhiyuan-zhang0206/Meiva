# meiva:note
# Main-red watch blindness thresholds (task #6523): how many streams must fail within one round, and
# how many consecutive rounds one stream may fail alone, before the watch says its own judgement
# stopped. Configuration rather than constants because both are behaviour a reader of the card has
# to trust, and because the cadence they are expressed in (rounds) is a property of the schedule
# that can change, not of the judgement (users' 2026-09-17 ruling: behaviour constants are
# configuration, and every limit carries its reason).

from __future__ import annotations

from pydantic import Field


class MainRedWatchFields:
    main_red_watch_blind_streams: int = Field(
        title="Unreadable streams that make one round blind",
        default=2,
        ge=1,
        alias="MEIVA_MAIN_RED_WATCH_BLIND_STREAMS",
        description=(
            "How many of the main-red watch's streams must fail to read within one round before "
            "the round opens its blindness card. One failing stream stays a note: the round judged "
            "the other three and a card per busy API would train the lead to ignore the title. Two "
            "or more is a round that judged almost nothing, where an empty card list stops meaning "
            "'nothing is red' and starts meaning 'nobody was reading'."
        ),
        json_schema_extra={
            "capability": "common",
            "restart_required": "schedule",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    main_red_watch_blind_stream_rounds: int = Field(
        title="Consecutive unreadable rounds before one stream is reported",
        default=6,
        ge=1,
        alias="MEIVA_MAIN_RED_WATCH_BLIND_STREAM_ROUNDS",
        description=(
            "How many consecutive rounds one stream may stay unreadable before the watch opens a "
            "card naming it, even while every other stream reads. Six rounds is three hours at the "
            "half-hourly cadence: short enough that a stream cannot rot through a night, long "
            "enough that one blip is not a card. Expressed in rounds because that is what the "
            "watch observes; a cron change that halves the cadence halves this window."
        ),
        json_schema_extra={
            "capability": "common",
            "restart_required": "schedule",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
