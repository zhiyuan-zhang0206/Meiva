from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.config.domains.daemon.settings import DaemonSettings


# meiva:note
# The watch's blindness thresholds, in the configuration layer rather than in the judgement (task
# #6523; users' 2026-09-17 ruling that behaviour constants are configuration). The defaults are the
# values the code shipped with, so a deployment that changes nothing keeps the behaviour the card's
# tests describe.
def test_the_blindness_defaults_are_the_shipped_ones() -> None:
    fields = DaemonSettings.model_fields

    assert fields["main_red_watch_blind_streams"].default == 2
    assert fields["main_red_watch_blind_stream_rounds"].default == 6


def test_the_blindness_thresholds_come_from_the_environment() -> None:
    config = DaemonSettings.model_validate(
        {
            "MEIVA_MAIN_RED_WATCH_BLIND_STREAMS": 3,
            "MEIVA_MAIN_RED_WATCH_BLIND_STREAM_ROUNDS": 2,
        }
    )

    assert config.main_red_watch_blind_streams == 3
    assert config.main_red_watch_blind_stream_rounds == 2


# meiva:note
# A threshold of zero would make every round a card and every stream a card on its first miss: the
# guard has to be rejected at configuration load, not silently treated as "always report".
@pytest.mark.parametrize(
    "alias",
    ["MEIVA_MAIN_RED_WATCH_BLIND_STREAMS", "MEIVA_MAIN_RED_WATCH_BLIND_STREAM_ROUNDS"],
)
def test_a_zero_threshold_is_refused(alias: str) -> None:
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({alias: 0})
