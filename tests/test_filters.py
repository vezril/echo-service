"""The metadata-level filters: the rules that decide whether bandwidth gets spent.

The three-way verdict is the thing under test. Conflating "not yet" with "never" either loses
a stream's finished recording forever or retries a deleted video until someone notices.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from echo.fetch.filters import (
    SHORTS_MAX_SECONDS,
    Verdict,
    VideoMetadata,
    is_short,
    judge,
    metadata_from_info,
)
from echo.follow.config import ChannelSettings

SettingsFactory = Callable[..., ChannelSettings]


def meta(**overrides: Any) -> VideoMetadata:
    base: dict[str, Any] = {
        "video_id": "dQw4w9WgXcQ",
        "title": "A Normal Review",
        "uploader": "Linus Tech Tips",
        "duration": timedelta(minutes=12),
        "width": 1920,
        "height": 1080,
    }
    return VideoMetadata(**{**base, **overrides})


# --- narrowing yt-dlp's info dict ------------------------------------------------------


def test_metadata_narrows_the_keys_echo_decides_on() -> None:
    parsed = metadata_from_info(
        {
            "id": "dQw4w9WgXcQ",
            "title": "  A Normal Review  ",
            "uploader": "Linus Tech Tips",
            "duration": 742.5,
            "width": 1920,
            "height": 1080,
            "is_live": False,
            "was_live": True,
            "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "a_hundred_other_keys": "ignored",
        }
    )

    assert parsed.video_id == "dQw4w9WgXcQ"
    assert parsed.title == "A Normal Review"
    assert parsed.duration == timedelta(seconds=742.5)
    assert parsed.was_live is True
    assert parsed.is_live is False


def test_metadata_tolerates_missing_optional_keys() -> None:
    """yt-dlp's keys move between releases; only the id is treated as required."""
    parsed = metadata_from_info({"id": "abc12345678"})

    assert parsed.video_id == "abc12345678"
    assert parsed.title == "abc12345678"  # falls back to the id rather than empty
    assert parsed.duration is None
    assert parsed.uploader is None
    assert parsed.width is None


def test_metadata_falls_back_to_channel_for_the_uploader() -> None:
    parsed = metadata_from_info({"id": "abc12345678", "channel": "Veritasium"})
    assert parsed.uploader == "Veritasium"


@pytest.mark.parametrize("info", [{}, {"id": ""}, {"id": 12345}])
def test_metadata_without_an_id_is_an_error(info: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="no usable 'id'"):
        metadata_from_info(info)


def test_a_boolean_duration_is_not_mistaken_for_a_number() -> None:
    assert metadata_from_info({"id": "abc12345678", "duration": True}).duration is None


# --- Shorts ---------------------------------------------------------------------------


def test_a_shorts_url_is_conclusive() -> None:
    assert is_short(meta(webpage_url="https://www.youtube.com/shorts/abc12345678")) is True


def test_a_short_portrait_clip_is_a_short() -> None:
    assert is_short(meta(width=1080, height=1920, duration=timedelta(seconds=30))) is True


def test_a_short_landscape_clip_is_not_a_short() -> None:
    """A 45-second landscape clip is a teaser, not a Short — min_duration's job, not this."""
    assert is_short(meta(width=1920, height=1080, duration=timedelta(seconds=45))) is False


def test_a_long_portrait_video_is_not_a_short() -> None:
    assert (
        is_short(meta(width=1080, height=1920, duration=timedelta(seconds=SHORTS_MAX_SECONDS + 1)))
        is False
    )


def test_an_unknown_duration_is_not_assumed_to_be_a_short() -> None:
    assert is_short(meta(duration=None, width=1080, height=1920)) is False


def test_shorts_are_rejected_unless_enabled(settings_for: SettingsFactory) -> None:
    short = meta(width=1080, height=1920, duration=timedelta(seconds=30))

    rejected = judge(short, settings_for(filters={"min_duration": None}))
    assert rejected.verdict is Verdict.REJECT
    assert "Short" in rejected.reason

    accepted = judge(short, settings_for(shorts=True, filters={"min_duration": None}))
    assert accepted.verdict is Verdict.ACCEPT


# --- livestreams ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["skip", "after"])
def test_a_live_stream_is_deferred_not_rejected(mode: str, settings_for: SettingsFactory) -> None:
    """Deferred, so the caller does not record it as seen and can collect the recording."""
    judgement = judge(meta(is_live=True, duration=None), settings_for(livestreams=mode))

    assert judgement.verdict is Verdict.DEFER
    assert judgement.retryable is True
    assert "live now" in judgement.reason


def test_livestreams_never_rejects_a_live_stream(settings_for: SettingsFactory) -> None:
    judgement = judge(meta(is_live=True, duration=None), settings_for(livestreams="never"))

    assert judgement.verdict is Verdict.REJECT
    assert judgement.retryable is False


def test_livestreams_never_also_rejects_the_finished_recording(
    settings_for: SettingsFactory,
) -> None:
    judgement = judge(meta(was_live=True), settings_for(livestreams="never"))
    assert judgement.verdict is Verdict.REJECT
    assert "past livestream" in judgement.reason


@pytest.mark.parametrize("mode", ["skip", "after"])
def test_a_finished_stream_is_an_ordinary_upload(mode: str, settings_for: SettingsFactory) -> None:
    judgement = judge(meta(was_live=True), settings_for(livestreams=mode))
    assert judgement.verdict is Verdict.ACCEPT


def test_livestream_state_is_judged_before_duration(settings_for: SettingsFactory) -> None:
    """A live stream has no meaningful duration, so the bounds must not see it first."""
    judgement = judge(
        meta(is_live=True, duration=timedelta(seconds=5)),
        settings_for(livestreams="skip", filters={"min_duration": "10m"}),
    )
    assert judgement.verdict is Verdict.DEFER


# --- duration bounds ------------------------------------------------------------------


def test_a_video_shorter_than_min_duration_is_rejected(settings_for: SettingsFactory) -> None:
    judgement = judge(
        meta(duration=timedelta(seconds=30)), settings_for(filters={"min_duration": "60s"})
    )
    assert judgement.verdict is Verdict.REJECT
    assert "shorter than min_duration (30s < 60s)" in judgement.reason


def test_a_video_longer_than_max_duration_is_rejected(settings_for: SettingsFactory) -> None:
    judgement = judge(
        meta(duration=timedelta(hours=4)), settings_for(filters={"max_duration": "3h"})
    )
    assert judgement.verdict is Verdict.REJECT
    assert "longer than max_duration" in judgement.reason


def test_the_bounds_are_inclusive(settings_for: SettingsFactory) -> None:
    settings = settings_for(filters={"min_duration": "60s", "max_duration": "10m"})
    assert judge(meta(duration=timedelta(seconds=60)), settings).verdict is Verdict.ACCEPT
    assert judge(meta(duration=timedelta(minutes=10)), settings).verdict is Verdict.ACCEPT


def test_an_unknown_duration_is_accepted_rather_than_lost(settings_for: SettingsFactory) -> None:
    """Rejecting would lose a real upload; deferring would retry it forever."""
    judgement = judge(meta(duration=None), settings_for(filters={"min_duration": "60s"}))

    assert judgement.verdict is Verdict.ACCEPT
    assert "duration unknown" in judgement.reason


def test_no_bounds_means_any_duration_passes(settings_for: SettingsFactory) -> None:
    settings = settings_for(filters={"min_duration": None, "max_duration": None})
    assert judge(meta(duration=timedelta(seconds=2)), settings).verdict is Verdict.ACCEPT


# --- title filters, re-applied ---------------------------------------------------------


def test_title_filters_are_re_applied_against_the_extracted_title(
    settings_for: SettingsFactory,
) -> None:
    """yt-dlp's title can differ from the feed's, and fetch/ must be safe to call directly."""
    settings = settings_for(filters={"title_exclude": "(?i)trailer"})
    judgement = judge(meta(title="Official Trailer"), settings)

    assert judgement.verdict is Verdict.REJECT
    assert "title filters" in judgement.reason


def test_an_accepted_video_says_so(settings: ChannelSettings) -> None:
    judgement = judge(meta(), settings)
    assert judgement.accepted is True
    assert judgement.retryable is False
